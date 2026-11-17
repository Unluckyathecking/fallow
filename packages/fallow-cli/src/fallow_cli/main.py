"""``flw`` — the Fallow admin command-line interface.

Talks only to the coordinator admin API (``/v1/admin/*``, see
``docs/admin-api.md``). Global options live on the root callback:
``--coordinator-url`` and ``--json``. The admin key is read from the
``FLW_ADMIN_KEY`` env var or the config file — never a flag.

Test seams: ``_ADMIN_TRANSPORT`` / ``_DOWNLOAD_TRANSPORT`` are monkeypatched
with ``httpx.MockTransport`` so no command ever touches the network in tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import httpx
import typer

from fallow_cli import render
from fallow_cli.blobs import BLOB_DIR
from fallow_cli.errors import CliError
from fallow_cli.models_cmd import models_app
from fallow_cli.site import preflight_destinations, write_join_bundles
from fallow_cli.site.status import fetch_fleet_status, render_fleet_status
from fallow_cli.state import (
    CliState,
    ModelIdOpt,
    cli_state,
    guard,
    guard_direct,
    guard_local,
)
from fallow_protocol import JobSubmit, WorkerKind

app = typer.Typer(name="flw", help="Fallow — opportunistic private AI compute layer.")
enroll_app = typer.Typer(help="Manage agent enrollment tokens.")
keys_app = typer.Typer(help="Manage client API keys.")
agents_app = typer.Typer(help="Inspect enrolled agents.")
site_app = typer.Typer(help="Manage LAN Site enrollment.")
jobs_app = typer.Typer(help="Submit and inspect batch jobs.")
app.add_typer(enroll_app, name="enroll")
app.add_typer(keys_app, name="keys")
app.add_typer(agents_app, name="agents")
app.add_typer(models_app, name="models")
app.add_typer(jobs_app, name="jobs")
app.add_typer(site_app, name="site")

# Test seams: default ``None`` uses httpx's real transport.
_ADMIN_TRANSPORT: httpx.BaseTransport | None = None
_DOWNLOAD_TRANSPORT: httpx.BaseTransport | None = None

# ── Reusable option/argument annotations ─────────────────────────────────────
UrlOpt = Annotated[str | None, typer.Option("--coordinator-url", help="Coordinator base URL.")]
JsonOpt = Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")]


@app.callback()
def _root(ctx: typer.Context, coordinator_url: UrlOpt = None, json_output: JsonOpt = False) -> None:
    """Fallow admin CLI. The admin key comes from FLW_ADMIN_KEY or the config file."""
    ctx.obj = CliState(
        coordinator_url=coordinator_url,
        json_output=json_output,
        admin_transport=_ADMIN_TRANSPORT,
        download_transport=_DOWNLOAD_TRANSPORT,
    )


# ── enroll ───────────────────────────────────────────────────────────────────
@enroll_app.command("new-token")
def enroll_new_token(ctx: typer.Context) -> None:
    """Mint a one-time agent enrollment token."""
    state = cli_state(ctx)
    with guard(state) as client:
        token = client.create_enrollment_token()
    render.emit_value("enrollment_token", token, state.json_output)


@enroll_app.command("list")
def enroll_list(ctx: typer.Context) -> None:
    """List minted enrollment tokens by id, with what became of each."""
    state = cli_state(ctx)
    with guard(state) as client:
        tokens = client.list_enrollment_tokens()
    render.render_enrollment_tokens(tokens, state.json_output)


@enroll_app.command("revoke")
def enroll_revoke(
    ctx: typer.Context,
    token_id: Annotated[str, typer.Argument(help="Token id from `enroll list` or the mint line.")],
) -> None:
    """Void an unused enrollment token so a join file carrying it cannot enrol."""
    state = cli_state(ctx)
    with guard(state) as client:
        client.revoke_enrollment_token(token_id)
    render.emit_value("revoked_enrollment_token", token_id, state.json_output)


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
    state = cli_state(ctx)
    allowlist = _split_csv(allow)
    with guard(state) as client:
        key = client.create_api_key(name, allowlist, rpm, per_day)
    render.emit_value("api_key", key, state.json_output)


# ── agents ───────────────────────────────────────────────────────────────────
@agents_app.command("list")
def agents_list(
    ctx: typer.Context,
    revoked: Annotated[
        bool, typer.Option("--revoked", help="List revoked agents instead of live ones.")
    ] = False,
) -> None:
    """List enrolled agents and their live state.

    A revoked agent leaves every routing view the moment it is revoked, so it is
    not in the default listing. `--revoked` is where it goes instead — without
    it, an agent someone revoked would be indistinguishable from one that was
    never enrolled.
    """
    state = cli_state(ctx)
    if revoked:
        with guard(state) as client:
            dead = client.list_revoked_agents()
        render.render_revoked_agents(dead, state.json_output)
        return
    with guard(state) as client:
        agents = client.list_agents()
    render.render_agents(agents, state.json_output)


@agents_app.command("revoke")
def agents_revoke(
    ctx: typer.Context,
    agent_id: Annotated[str, typer.Argument(help="Agent whose device token to revoke.")],
) -> None:
    """Revoke an agent's device token. Terminal: that identity never serves again."""
    state = cli_state(ctx)
    with guard(state) as client:
        client.revoke_agent(agent_id)
    render.emit_value("revoked_agent", agent_id, state.json_output)


# ── assign ───────────────────────────────────────────────────────────────────
@app.command("assign")
def assign(
    ctx: typer.Context,
    model_id: Annotated[str, typer.Argument(help="Model to assign.")],
    agent_ids: Annotated[
        list[str] | None, typer.Argument(help="Agents that should serve it.")
    ] = None,
    fit: Annotated[
        bool,
        typer.Option("--fit", help="Assign to every live, unassigned agent the model fits."),
    ] = False,
) -> None:
    """Set the exact set of agents assigned to serve a model, or sweep with --fit."""
    state = cli_state(ctx)
    with guard_local(state):
        if fit == bool(agent_ids):
            raise CliError("pass agent ids or --fit, one or the other")
    if fit:
        with guard(state) as client:
            result = client.fit_assignments(model_id)
        render.render_fit_assignments(result, state.json_output)
        return
    with guard(state) as client:
        client.set_assignments(model_id, tuple(agent_ids or ()))
    render.emit_value("assigned", model_id, state.json_output)


# ── jobs ─────────────────────────────────────────────────────────────────────
@jobs_app.command("submit")
def jobs_submit(
    ctx: typer.Context,
    kind: Annotated[WorkerKind, typer.Option("--kind")],
    model_id: ModelIdOpt,
    payload_ref: Annotated[str, typer.Option("--payload-ref")],
    priority: Annotated[int, typer.Option("--priority")] = 0,
    assign_fit: Annotated[
        bool,
        typer.Option(
            "--assign-fit",
            help="After submitting, assign the job's model to every live, unassigned "
            "agent it fits.",
        ),
    ] = False,
) -> None:
    """Submit a batch job; the coordinator splits it into work units."""
    state = cli_state(ctx)
    job = JobSubmit(kind=kind, model_id=model_id, payload_ref=payload_ref, priority=priority)
    sweep: dict[str, object] | None = None
    sweep_error: CliError | None = None
    with guard(state) as client:
        status = client.submit_job(job)
        if assign_fit:
            # The job is committed; a sweep failure must not swallow its id.
            try:
                sweep = client.fit_assignments(model_id)
            except CliError as exc:
                sweep_error = exc
    if sweep is not None and state.json_output:
        # One top-level document, so `--json` stdout stays parseable.
        render.print_json({"job": status.model_dump(mode="json"), "fit": sweep})
    else:
        render.render_job(status, state.json_output)
        if sweep is not None:
            render.render_fit_assignments(sweep, state.json_output)
    if sweep_error is not None:
        typer.echo(sweep_error.message, err=True)
        typer.echo(
            f"job {status.job_id} was submitted; rerun the sweep with"
            f" `flw assign {model_id} --fit`",
            err=True,
        )
        raise typer.Exit(sweep_error.exit_code)


@jobs_app.command("status")
def jobs_status(
    ctx: typer.Context,
    job_id: Annotated[str, typer.Argument(help="Job id returned by `jobs submit`.")],
) -> None:
    """Show the current status of a batch job."""
    state = cli_state(ctx)
    with guard(state) as client:
        status = client.get_job(job_id)
    render.render_job(status, state.json_output)


# ── status ───────────────────────────────────────────────────────────────────
@app.command("status")
def status(ctx: typer.Context) -> None:
    """Show a one-glance summary of agents and models."""
    state = cli_state(ctx)
    with guard(state) as client:
        agents = client.list_agents()
        models = client.list_models()
    render.render_status(agents, models, state.json_output)


def _split_csv(raw: str | None) -> tuple[str, ...] | None:
    if raw is None:
        return None
    items = tuple(part.strip() for part in raw.split(",") if part.strip())
    return items or None


@site_app.command("join-bundles")
def site_join_bundles(
    ctx: typer.Context,
    count: Annotated[int, typer.Option("--count", min=1, max=16)],
    output: Annotated[Path, typer.Option("--output")],
    force: Annotated[bool, typer.Option("--force")] = False,
) -> None:
    """Write one single-use, per-device Site Mode join file per bundle."""
    state = cli_state(ctx)
    with guard_local(state):
        # Refuse a clobber before any one-use token is minted; the write path
        # re-checks atomically to close the preflight-to-write race.
        preflight_destinations(output, count, force=force)
    with guard(state, direct=True) as client:
        bundles = client.create_site_join_bundles(count)
    try:
        metadata = write_join_bundles(bundles, output, force=force)
    except CliError as exc:
        typer.echo(exc.message, err=True)
        raise typer.Exit(exc.exit_code) from exc
    if state.json_output:
        render.print_json(metadata)
    else:
        for item in metadata:
            origins = ",".join(item["coordinator_urls"])
            typer.echo(
                f"{item['path']} site={item['site_id']} origins={origins} "
                f"pin={item['pin_prefix']} token={item['token_id']}"
            )


@site_app.command("status")
def site_status(ctx: typer.Context) -> None:
    """Show every Site Mode agent's live enrollment, presence and claim state."""
    state = cli_state(ctx)
    with guard_direct(state) as (client, key):
        agents = fetch_fleet_status(client, key)
    render_fleet_status(agents, state.json_output)


__all__ = ["BLOB_DIR", "app"]
