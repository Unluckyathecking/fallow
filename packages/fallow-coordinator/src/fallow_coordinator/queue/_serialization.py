"""Pure (de)serialization helpers shared by the store.

No I/O, no clocks — deterministic and unit-testable in isolation.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Final

from fallow_coordinator.queue._constants import DEFAULT_LEASE_S, LEASE_EST_MULTIPLIER
from fallow_protocol.messages import WorkMetrics, WorkResult

# ISO-8601 with a fixed 6-digit fractional second so every stored timestamp has
# the same width; that makes lexicographic ordering == chronological ordering.
_ISO_TIMESPEC: Final[str] = "microseconds"


def to_iso(moment: datetime) -> str:
    """Render an aware datetime as a fixed-width UTC ISO-8601 string."""
    return moment.astimezone(UTC).isoformat(timespec=_ISO_TIMESPEC)


def lease_window_s(est_duration_s: float | None, default_lease_s: float) -> float:
    """Lease duration for a unit: whichever is larger of a multiple of the
    estimate or the configured floor."""
    estimated = (est_duration_s or 0.0) * LEASE_EST_MULTIPLIER
    return max(estimated, default_lease_s)


def lease_expiry(now: datetime, est_duration_s: float | None, default_lease_s: float) -> datetime:
    """Absolute lease-expiry instant for a newly leased unit."""
    return now.astimezone(UTC) + timedelta(seconds=lease_window_s(est_duration_s, default_lease_s))


def dump_params(params: dict[str, str]) -> str:
    """Serialize job params to canonical JSON (sorted keys for stability)."""
    return json.dumps(params, sort_keys=True, separators=(",", ":"))


def dump_metrics(metrics: WorkMetrics | None) -> str | None:
    """Serialize optional work metrics to JSON, or None."""
    return None if metrics is None else metrics.model_dump_json()


def result_row_params(result: WorkResult, agent_id: str, completed_at: str) -> dict[str, object]:
    """Bind parameters for an ``INSERT INTO unit_results``."""
    return {
        "work_unit_id": result.work_unit_id,
        "status": result.status.value,
        "result_ref": result.result_ref,
        "error": result.error,
        "metrics_json": dump_metrics(result.metrics),
        "agent_id": agent_id,
        "completed_at": completed_at,
    }


__all__ = [
    "DEFAULT_LEASE_S",
    "dump_metrics",
    "dump_params",
    "lease_expiry",
    "lease_window_s",
    "result_row_params",
    "to_iso",
]
