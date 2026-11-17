"""``flw models`` — list, register and pull models."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from fallow_cli import pull, render
from fallow_cli.blobs import build_manifest, dest_for, download_to
from fallow_cli.state import (
    ModelIdOpt,
    cli_state,
    guard,
    guard_local,
    make_download_client,
)
from fallow_protocol import WorkerKind

models_app = typer.Typer(help="Manage registered models.")
_stderr = Console(stderr=True)

FamilyOpt = Annotated[str, typer.Option("--family")]
QuantOpt = Annotated[str, typer.Option("--quant")]
KindOpt = Annotated[WorkerKind, typer.Option("--worker-kind")]
VramOpt = Annotated[int, typer.Option("--min-vram-mb")]
RamOpt = Annotated[int, typer.Option("--min-ram-mb")]

# ``models pull`` derives what it can, so every metadata flag there is optional
# and ``None`` means "not stated" rather than a usable default.
SourceArg = Annotated[
    str | None,
    typer.Argument(help="URL, or hf:<owner>/<repo>/<file.gguf> with an optional @<revision>."),
]
CatalogOpt = Annotated[str | None, typer.Option("--catalog", help="Curated catalog entry id.")]
ModelIdOptOpt = Annotated[str | None, typer.Option("--model-id")]
FamilyOptOpt = Annotated[str | None, typer.Option("--family")]
QuantOptOpt = Annotated[str | None, typer.Option("--quant")]
KindOptOpt = Annotated[WorkerKind | None, typer.Option("--worker-kind")]
VramOptOpt = Annotated[int | None, typer.Option("--min-vram-mb")]
RamOptOpt = Annotated[int | None, typer.Option("--min-ram-mb")]


@models_app.command("list")
def models_list(ctx: typer.Context) -> None:
    """List registered models."""
    state = cli_state(ctx)
    with guard(state) as client:
        models = client.list_models()
    render.render_models(models, state.json_output)


@models_app.command("register")
def models_register(
    ctx: typer.Context,
    file: Annotated[Path, typer.Option("--file", help="Local model blob on the coordinator host.")],
    model_id: ModelIdOpt,
    family: FamilyOpt,
    quant: QuantOpt,
    worker_kind: KindOpt = WorkerKind.CHAT,
    min_vram_mb: VramOpt = 0,
    min_ram_mb: RamOpt = 0,
) -> None:
    """Hash a local blob, build its manifest, and register it (v0.1: CLI runs on
    the coordinator host, so blob_path is sent verbatim)."""
    state = cli_state(ctx)
    with guard_local(state):
        manifest = build_manifest(
            path=file,
            model_id=model_id,
            family=family,
            quant=quant,
            worker_kind=worker_kind,
            min_ram_mb=min_ram_mb,
            min_vram_mb=min_vram_mb,
        )
    with guard(state) as client:
        client.register_model(manifest, str(file.resolve()))
    render.emit_value("registered", manifest.model_id, state.json_output)


@models_app.command("pull")
def models_pull(
    ctx: typer.Context,
    source: SourceArg = None,
    catalog: CatalogOpt = None,
    model_id: ModelIdOptOpt = None,
    family: FamilyOptOpt = None,
    quant: QuantOptOpt = None,
    worker_kind: KindOptOpt = None,
    min_vram_mb: VramOptOpt = None,
    min_ram_mb: RamOptOpt = None,
) -> None:
    """Download a blob into ~/.fallow/blobs, then register it like `register`.

    The source is a plain URL, an `hf:<owner>/<repo>/<file.gguf>` spec with an
    optional trailing `@<revision>`, or `--catalog <id>` for a curated entry.
    Anything not given is taken from the catalog entry, then from the downloaded
    file's GGUF header (`--quant`) and its size (`--min-ram-mb`). Flags win.
    """
    state = cli_state(ctx)
    with guard_local(state):
        plan = pull.plan_source(source, catalog)
        overrides = pull.Overrides(
            model_id=model_id,
            family=family,
            quant=quant,
            worker_kind=worker_kind,
            min_ram_mb=min_ram_mb,
            min_vram_mb=min_vram_mb,
        )
        # A bare `hf:` pull needs an id and a family from the operator, and no
        # download can supply either. Say so now rather than after the bytes.
        pull.preflight(plan, overrides)
        dest = dest_for(plan.url, model_id or catalog or "model.gguf")
        with make_download_client(state) as dl:
            part = download_to(dl, plan.url, dest, _stderr)
        # Everything from here to the promote works on the unverified part, and
        # every failure takes only that with it. Resolution can still fail after
        # the bytes land — an unmapped ftype and no --quant is the usual way —
        # and nothing resumes a half-finished pull, so keeping a multi-GB file
        # for a manifest that was never built only costs the operator disk. The
        # manifest carries the destination's name, which is what gets registered.
        fields = pull.resolve_fields_or_discard(plan, part, overrides)
        manifest = build_manifest(
            path=part,
            file_name=dest.name,
            model_id=fields.model_id,
            family=fields.family,
            quant=fields.quant,
            worker_kind=fields.worker_kind,
            min_ram_mb=fields.min_ram_mb,
            min_vram_mb=fields.min_vram_mb,
            source_url=plan.url,
            license=fields.license,
        )
        pull.verify(manifest, plan, part)
        # Verified, so it takes the name. os.replace within one directory is
        # atomic: a re-pull that failed anywhere above leaves the blob already
        # registered exactly as it was, and one that got here swaps it in whole.
        # But the swap must not outrun the registry. On a re-pull the bytes at
        # dest are what the registered manifest's sha256 promises, so if the
        # register below fails after the swap, agents would pull the new bytes
        # against the old hash and the working model goes dark. Set the old
        # blob aside until the coordinator has accepted the new manifest.
        prev = dest.with_name(dest.name + ".prev")
        replacing = dest.exists()
        if replacing:
            dest.replace(prev)
        part.replace(dest)
    try:
        with guard(state) as client:
            client.register_model(manifest, str(dest.resolve()))
    except BaseException:
        # The registry still points at the old manifest; put its bytes back.
        # A first-time pull has nothing to restore and keeps the blob, exactly
        # as a failed first registration always has.
        if replacing:
            prev.replace(dest)
        raise
    if replacing:
        prev.unlink()
    # Plain echo, not the rich console: this line is a record, and rich would
    # wrap and highlight it at the terminal width.
    typer.echo(pull.provenance_line(manifest, plan), err=True)
    render.emit_value("registered", manifest.model_id, state.json_output)
