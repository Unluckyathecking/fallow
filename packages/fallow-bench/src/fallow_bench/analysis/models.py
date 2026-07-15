"""Frozen configuration and container types for the B3 analysis pipeline.

Everything a run needs to be reduced deterministically is declared here: the
idle-power baseline window, the JSONL file names, the float precision, and the
report metadata. No wall-clock or randomness lives anywhere in analysis — these
values are injected so the same logs always render the same report.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd  # type: ignore[import-untyped]

from fallow_protocol.base import FallowModel


class EnergyBaseline(FallowModel):
    """The idle window (in the power log's own time units) whose mean draw is
    treated as each agent's baseline and subtracted before integrating."""

    start_s: float
    end_s: float


class AnalysisConfig(FallowModel):
    """All knobs the reducer needs. File names are config, not hardcoded, so an
    experiment can rename its logs without touching code."""

    energy_baseline: EnergyBaseline
    client_trace_name: str = "client_trace.jsonl"
    gateway_name: str = "gateway.jsonl"
    events_name: str = "events.jsonl"
    churn_name: str = "churn.jsonl"
    power_name: str = "power.jsonl"
    jobs_name: str = "job_status.jsonl"
    float_precision: int = 3


class ReportMeta(FallowModel):
    """Run metadata stamped into the report. Deliberately carries NO timestamp:
    determinism forbids wall-clock in output."""

    title: str = "Fallow scheduling experiment"
    git_sha: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class RunFrames:
    """The six parsed logs of a single arm plus every warning raised loading
    them. Frozen: treated as read-only downstream."""

    client: pd.DataFrame
    gateway: pd.DataFrame
    events: pd.DataFrame
    churn: pd.DataFrame
    power: pd.DataFrame
    jobs: pd.DataFrame
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class MetricRow:
    """One headline-table row: a label, a unit, and one value per arm (``None``
    renders as an em dash)."""

    label: str
    values: tuple[float | None, ...]


@dataclass(frozen=True)
class MetricTable:
    """The full cross-arm table: arm labels (sorted) and the ordered rows."""

    arms: tuple[str, ...]
    rows: tuple[MetricRow, ...]
