"""``flw`` — the Fallow admin command-line interface.

Talks only to the coordinator admin API (``/v1/admin/*``, see
``docs/admin-api.md``). Global options live on the root callback:
``--coordinator-url`` and ``--json``. The admin key is read from the
``FLW_ADMIN_KEY`` env var or the config file — never a flag.

Test seams: ``_ADMIN_TRANSPORT`` / ``_DOWNLOAD_TRANSPORT`` are monkeypatched
with ``httpx.MockTransport`` so no command ever touches the network in tests.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import httpx
import typer
from rich.console import Console

from fallow_cli import render
from fallow_cli.blobs import BLOB_DIR, build_manifest, dest_for, download_to
from fallow_cli.client import AdminClient
from fallow_cli.config import CliConfig, load_config, require_admin_key
from fallow_cli.errors import CliError
from fallow_protocol import JobSubmit, WorkerKind

app = typer.Typer(name="flw", help="Fallow — opportunistic private AI compute layer.")
enroll_app = typer.Typer(help="Manage agent enrollment tokens.")
keys_app = typer.Typer(help="Manage client API keys.")
agents_app = typer.Typer(help="Inspect enrolled agents.")
models_app = typer.Typer(help="Manage registered models.")
jobs_app = typer.Typer(help="Submit and inspect batch jobs.")
app.add_typer(enroll_app, name="enroll")
app.add_typer(keys_app, name="keys")
app.add_typer(agents_app, name="agents")
app.add_typer(models_app, name="models")
app.add_typer(jobs_app, name="jobs")

# Test seams: default ``None`` uses httpx's real transport.
_ADMIN_TRANSPORT: httpx.BaseTransport | None = None
_DOWNLOAD_TRANSPORT: httpx.BaseTransport | None = None
_HTTP_TIMEOUT = httpx.Timeout(30.0)
_stderr = Console(stderr=True)

# ── Reusable option/argument annotations ─────────────────────────────────────
UrlOpt = Annotated[str | None, typer.Option("--coordinator-url", help="Coordinator base URL.")]
JsonOpt = Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")]
ModelIdOpt = Annotated[str, typer.Option("--model-id")]
FamilyOpt = Annotated[str, typer.Option("--family")]
QuantOpt = Annotated[str, typer.Option("--quant")]
KindOpt = Annotated[WorkerKind, typer.Option("--worker-kind")]
VramOpt = Annotated[int, typer.Option("--min-vram-mb")]
RamOpt = Annotated[int, typer.Option("--min-ram-mb")]


@dataclass(frozen=True)
class CliState:
    """Resolved global options, attached to the typer context."""

    coordinator_url: str | None
    json_output: bool


@app.callback()
def _root(ctx: typer.Context, coordinator_url: UrlOpt = None, json_output: JsonOpt = False) -> None:
    """Fallow admin CLI. The admin key comes from FLW_ADMIN_KEY or the config file."""
    ctx.obj = CliState(coordinator_url=coordinator_url, json_output=json_output)


# ── Shared plumbing ──────────────────────────────────────────────────────────
def _state(ctx: typer.Context) -> CliState:
    obj = ctx.obj
    if not isinstance(obj, CliState):  # pragma: no cover - defensive
        raise CliError("internal error: CLI state not initialised")
    return obj


def _resolve(state: CliState) -> CliConfig:
    return load_config(state.coordinator_url, dict(os.environ))


def _make_admin_client(config: CliConfig) -> AdminClient:
    key = require_admin_key(config)
    client = httpx.Client(
        base_url=config.coordinator_url, timeout=_HTTP_TIMEOUT, transport=_ADMIN_TRANSPORT
    )
    return AdminClient(client, key)


def _make_download_client() -> httpx.Client:
    return httpx.Client(
        follow_redirects=True,
        timeout=httpx.Timeout(30.0, read=None),
        transport=_DOWNLOAD_TRANSPORT,
    )


@contextmanager
def _guard(state: CliState) -> Iterator[AdminClient]:
    """Build the admin client and translate CliError into a clean exit."""
    try:
        with _make_admin_client(_resolve(state)) as client:
            yield client
    except CliError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(exc.exit_code) from exc


@contextmanager
def _guard_local(state: CliState) -> Iterator[None]:
    """Guard commands that do local work before any admin client is built."""
    try:
        yield
    except CliError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(exc.exit_code) from exc


# ── enroll ───────────────────────────────────────────────────────────────────
@enroll_app.command("new-token")
def enroll_new_token(ctx: typer.Context) -> None:
    """Mint a one-time agent enrollment token."""
    state = _state(ctx)
    with _guard(state) as client:
        token = client.create_enrollment_token()
    render.emit_value("enrollment_token", token, state.json_output)


# ── keys ─────────────────────────────────────────────────────────────────────
@keys_app.command("new")
def keys_new(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Human label for the key.")],
    allow: Annotated[
        str | None, typer.Option("--allow", help="Comma-separated model_ids (default: all).")
    ] = None,
    rpm: Annotated[int | None, typer.Option("--rpm", min=1, help="Requests per minute.")] = None,
    per_day: Annotated[
        int | None, typer.Option("--per-day", min=1, help="Requests per UTC day.")
    ] = None,
) -> None:
    """Create a client API key, optionally restricted to an allowlist."""
    state = _state(ctx)
    allowlist = _split_csv(allow)
    with _guard(state) as client:
        key = client.create_api_key(name, allowlist, rpm, per_day)
    render.emit_value("api_key", key, state.json_output)


# ── agents ───────────────────────────────────────────────────────────────────
@agents_app.command("list")
def agents_list(ctx: typer.Context) -> None:
    """List enrolled agents and their live state."""
    state = _state(ctx)
    with _guard(state) as client:
        agents = client.list_agents()
    render.render_agents(agents, state.json_output)


# ── models ───────────────────────────────────────────────────────────────────
@models_app.command("list")
def models_list(ctx: typer.Context) -> None:
    """List registered models."""
    state = _state(ctx)
    with _guard(state) as client:
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
    state = _state(ctx)
    with _guard_local(state):
        manifest = build_manifest(
            path=file,
            model_id=model_id,
            family=family,
            quant=quant,
            worker_kind=worker_kind,
            min_ram_mb=min_ram_mb,
            min_vram_mb=min_vram_mb,
        )
    with _guard(state) as client:
        client.register_model(manifest, str(file.resolve()))
    render.emit_value("registered", manifest.model_id, state.json_output)


@models_app.command("pull")
def models_pull(
    ctx: typer.Context,
    url: Annotated[str, typer.Argument(help="Source URL to stream the blob from.")],
    model_id: ModelIdOpt,
    family: FamilyOpt,
    quant: QuantOpt,
    worker_kind: KindOpt = WorkerKind.CHAT,
    min_vram_mb: VramOpt = 0,
    min_ram_mb: RamOpt = 0,
) -> None:
    """Download a blob into ~/.fallow/blobs, then register it like `register`."""
    state = _state(ctx)
    with _guard_local(state):
        dest = dest_for(url, model_id)
        with _make_download_client() as dl:
            path = download_to(dl, url, dest, _stderr)
        manifest = build_manifest(
            path=path,
            model_id=model_id,
            family=family,
            quant=quant,
            worker_kind=worker_kind,
            min_ram_mb=min_ram_mb,
            min_vram_mb=min_vram_mb,
            source_url=url,
        )
    with _guard(state) as client:
        client.register_model(manifest, str(path.resolve()))
    render.emit_value("registered", manifest.model_id, state.json_output)


# ── assign ───────────────────────────────────────────────────────────────────
@app.command("assign")
def assign(
    ctx: typer.Context,
    model_id: Annotated[str, typer.Argument(help="Model to assign.")],
    agent_ids: Annotated[list[str], typer.Argument(help="Agents that should serve it.")],
) -> None:
    """Set the exact set of agents assigned to serve a model."""
    state = _state(ctx)
    with _guard(state) as client:
        client.set_assignments(model_id, tuple(agent_ids))
    render.emit_value("assigned", model_id, state.json_output)


# ── jobs ─────────────────────────────────────────────────────────────────────
@jobs_app.command("submit")
def jobs_submit(
    ctx: typer.Context,
    kind: Annotated[WorkerKind, typer.Option("--kind")],
    model_id: ModelIdOpt,
    payload_ref: Annotated[str, typer.Option("--payload-ref")],
    priority: Annotated[int, typer.Option("--priority")] = 0,
) -> None:
    """Submit a batch job; the coordinator splits it into work units."""
    state = _state(ctx)
    job = JobSubmit(kind=kind, model_id=model_id, payload_ref=payload_ref, priority=priority)
    with _guard(state) as client:
        status = client.submit_job(job)
    render.render_job(status, state.json_output)


@jobs_app.command("status")
def jobs_status(
    ctx: typer.Context,
    job_id: Annotated[str, typer.Argument(help="Job id returned by `jobs submit`.")],
) -> None:
    """Show the current status of a batch job."""
    state = _state(ctx)
    with _guard(state) as client:
        status = client.get_job(job_id)
    render.render_job(status, state.json_output)


# ── status ───────────────────────────────────────────────────────────────────
@app.command("status")
def status(ctx: typer.Context) -> None:
    """Show a one-glance summary of agents and models."""
    state = _state(ctx)
    with _guard(state) as client:
        agents = client.list_agents()
        models = client.list_models()
    render.render_status(agents, models, state.json_output)


def _split_csv(raw: str | None) -> tuple[str, ...] | None:
    if raw is None:
        return None
    items = tuple(part.strip() for part in raw.split(",") if part.strip())
    return items or None


__all__ = ["BLOB_DIR", "app"]
