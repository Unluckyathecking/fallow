"""Output rendering: rich tables for humans, JSON for machines.

Every command routes through here so the global ``--json`` flag is honoured
uniformly. Human tables go to stdout via a shared :class:`Console`; JSON is
emitted with :func:`print_json`. Pydantic ``model_dump(mode="json")`` gives
stable, wire-shaped dicts (enums as values, datetimes as ISO strings).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from rich.console import Console
from rich.table import Table

from fallow_protocol import AgentSnapshot, JobStatus, ModelManifest

_stdout = Console()


def print_json(payload: Any) -> None:
    """Emit compact, deterministic JSON to stdout."""
    print(json.dumps(payload, indent=2, sort_keys=True))


def _dumps(models: Sequence[Any]) -> list[dict[str, Any]]:
    return [m.model_dump(mode="json") for m in models]


def render_agents(agents: Sequence[AgentSnapshot], as_json: bool) -> None:
    if as_json:
        print_json(_dumps(agents))
        return
    table = Table(title="agents")
    for column in ("id", "host", "state", "suspect", "models", "idle_s"):
        table.add_column(column)
    for agent in agents:
        models = ",".join(r.model_id for r in agent.replicas) or "-"
        table.add_row(
            agent.agent_id,
            agent.host,
            agent.state.value,
            "yes" if agent.suspect else "no",
            models,
            f"{agent.user_idle_s:.0f}",
        )
    _stdout.print(table)


def render_models(models: Sequence[ModelManifest], as_json: bool) -> None:
    if as_json:
        print_json(_dumps(models))
        return
    table = Table(title="models")
    for column in ("model_id", "family", "quant", "kind", "size_mb", "sha256"):
        table.add_column(column)
    for model in models:
        table.add_row(
            model.model_id,
            model.family,
            model.quant,
            model.worker_kind.value,
            f"{model.size_bytes / 1_000_000:.0f}",
            model.sha256[:12],
        )
    _stdout.print(table)


def render_job(status: JobStatus, as_json: bool) -> None:
    if as_json:
        print_json(status.model_dump(mode="json"))
        return
    table = Table(title=f"job {status.job_id}")
    for column in ("state", "total", "done", "dead"):
        table.add_column(column)
    table.add_row(
        status.state.value,
        str(status.total_units),
        str(status.done_units),
        str(status.dead_units),
    )
    _stdout.print(table)


def render_status(
    agents: Sequence[AgentSnapshot], models: Sequence[ModelManifest], as_json: bool
) -> None:
    if as_json:
        print_json({"agents": _dumps(agents), "models": _dumps(models)})
        return
    table = Table(title="coordinator status")
    table.add_column("metric")
    table.add_column("value")
    active = sum(1 for a in agents if a.state.value == "active")
    idle = sum(1 for a in agents if a.state.value == "idle")
    table.add_row("agents", str(len(agents)))
    table.add_row("  idle", str(idle))
    table.add_row("  active", str(active))
    table.add_row("models", str(len(models)))
    _stdout.print(table)


def emit_value(label: str, value: str, as_json: bool) -> None:
    """Render a single scalar result (a token, a key, a confirmation)."""
    if as_json:
        print_json({label: value})
        return
    # Plain print (no rich highlighting) so tokens/keys pipe cleanly.
    print(f"{label}: {value}")
