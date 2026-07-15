"""Batch throughput and on-prem service-rate metrics.

``units_per_hour`` reduces the per-unit job-status log; ``pct_served_on_prem``
reduces the gateway log's terminal ``status``. Both return ``None`` when the
input cannot support the ratio (no completions, zero time span, no requests).
"""

from __future__ import annotations

import pandas as pd  # type: ignore[import-untyped]

DONE_STATE = "done"
SECONDS_PER_HOUR = 3600.0
SERVED = "served"
SHED = "shed"
ERROR = "error"


def units_per_hour(jobs: pd.DataFrame) -> float | None:
    """Completed work units divided by the job log's elapsed hours."""
    if jobs.empty:
        return None
    times = [float(t) for t in jobs["t"].tolist() if t is not None]
    if len(times) < 2:
        return None
    span_s = max(times) - min(times)
    if span_s <= 0:
        return None
    done_units = {
        row.get("work_unit_id")
        for row in jobs.to_dict("records")
        if row.get("state") == DONE_STATE and row.get("work_unit_id") is not None
    }
    return len(done_units) / (span_s / SECONDS_PER_HOUR)


def pct_served_on_prem(gateway: pd.DataFrame) -> float | None:
    """Percentage of interactive requests served locally rather than shed/errored."""
    if gateway.empty:
        return None
    counts = {SERVED: 0, SHED: 0, ERROR: 0}
    for status in gateway["status"].tolist():
        if status in counts:
            counts[status] += 1
    total = counts[SERVED] + counts[SHED] + counts[ERROR]
    if total == 0:
        return None
    return 100.0 * counts[SERVED] / total
