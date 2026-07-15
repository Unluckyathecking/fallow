"""One pure loader per log kind: ``Path -> (DataFrame, warnings)``.

Every loader is total — a missing file returns an empty frame that still carries
the expected columns, so downstream metrics see a well-shaped (if empty) table
instead of a ``KeyError``. Times are normalised to float seconds via
:func:`fallow_bench.analysis.jsonl.to_seconds`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd  # type: ignore[import-untyped]

from fallow_bench.analysis.jsonl import read_jsonl, to_seconds
from fallow_bench.analysis.models import AnalysisConfig, RunFrames

CLIENT_COLS = ["req_id", "t_submit", "t_first_token", "t_done", "status", "tokens_out"]
GATEWAY_COLS = ["status"]
EVENT_COLS = ["agent_id", "kind", "at", "yield_ms"]
CHURN_COLS = ["t", "agent_id", "action", "flip_latency_ms"]
POWER_COLS = ["t", "agent_id", "watts"]
JOB_COLS = ["work_unit_id", "job_id", "agent_id", "attempt", "state", "t"]


def _frame(rows: list[dict[str, Any]], columns: list[str]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=columns)


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def load_client_trace(path: Path) -> tuple[pd.DataFrame, list[str]]:
    """Per-request client trace (B1)."""
    records, warnings = read_jsonl(path)
    rows = [
        {
            "req_id": r.get("req_id"),
            "t_submit": to_seconds(r.get("t_submit")),
            "t_first_token": to_seconds(r.get("t_first_token")),
            "t_done": to_seconds(r.get("t_done")),
            "status": r.get("status"),
            "tokens_out": _num(r.get("tokens_out")),
        }
        for r in records
    ]
    return _frame(rows, CLIENT_COLS), warnings


def load_gateway(path: Path) -> tuple[pd.DataFrame, list[str]]:
    """Coordinator gateway log (``GatewayLogEntry``); only ``status`` is read."""
    records, warnings = read_jsonl(path)
    rows = [{"status": r.get("status")} for r in records]
    return _frame(rows, GATEWAY_COLS), warnings


def load_events(path: Path) -> tuple[pd.DataFrame, list[str]]:
    """Coordinator agent events (``AgentEvent``); pulls ``detail.yield_ms``."""
    records, warnings = read_jsonl(path)
    rows = [
        {
            "agent_id": r.get("agent_id"),
            "kind": r.get("kind"),
            "at": to_seconds(r.get("at")),
            "yield_ms": _num(_detail(r).get("yield_ms")),
        }
        for r in records
    ]
    return _frame(rows, EVENT_COLS), warnings


def _detail(record: dict[str, Any]) -> dict[str, Any]:
    detail = record.get("detail")
    if isinstance(detail, dict):
        # detail values are strings on the wire; coerce numeric-looking ones.
        return {k: _maybe_float(v) for k, v in detail.items()}
    return {}


def _maybe_float(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return value
    return value


def load_churn(path: Path) -> tuple[pd.DataFrame, list[str]]:
    """Fleet-churn log (B2).

    Producer truth is B2's ChurnRecord (t_executed/agent/kind/flip_ms); the
    generic column names are accepted as a fallback for synthetic fixtures.
    """
    records, warnings = read_jsonl(path)
    rows = [
        {
            "t": to_seconds(r.get("t_executed", r.get("t"))),
            "agent_id": r.get("agent", r.get("agent_id")),
            "action": r.get("kind", r.get("action")),
            "flip_latency_ms": _num(r.get("flip_ms", r.get("flip_latency_ms"))),
        }
        for r in records
    ]
    return _frame(rows, CHURN_COLS), warnings


def load_power(path: Path) -> tuple[pd.DataFrame, list[str]]:
    """Per-agent power samples (B1 sampler).

    Producer truth is B1's PowerSample (power_w); "watts" accepted as fallback.
    """
    records, warnings = read_jsonl(path)
    rows = [
        {
            "t": to_seconds(r.get("t")),
            "agent_id": r.get("agent_id"),
            "watts": _num(r.get("power_w", r.get("watts"))),
        }
        for r in records
    ]
    return _frame(rows, POWER_COLS), warnings


def load_jobs(path: Path) -> tuple[pd.DataFrame, list[str]]:
    """Per-work-unit batch job-status log."""
    records, warnings = read_jsonl(path)
    rows = [
        {
            "work_unit_id": r.get("work_unit_id"),
            "job_id": r.get("job_id"),
            "agent_id": r.get("agent_id"),
            "attempt": _num(r.get("attempt")),
            "state": r.get("state"),
            "t": to_seconds(r.get("t")),
        }
        for r in records
    ]
    return _frame(rows, JOB_COLS), warnings


def load_run(run_dir: Path, config: AnalysisConfig) -> RunFrames:
    """Load every log in one arm's directory into a :class:`RunFrames`."""
    client, w1 = load_client_trace(run_dir / config.client_trace_name)
    gateway, w2 = load_gateway(run_dir / config.gateway_name)
    events, w3 = load_events(run_dir / config.events_name)
    churn, w4 = load_churn(run_dir / config.churn_name)
    power, w5 = load_power(run_dir / config.power_name)
    jobs, w6 = load_jobs(run_dir / config.jobs_name)
    warnings = tuple(w1 + w2 + w3 + w4 + w5 + w6)
    return RunFrames(client, gateway, events, churn, power, jobs, warnings)
