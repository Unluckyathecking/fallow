"""Assemble the headline metric table from per-arm :class:`RunFrames`.

The row order and labels here ARE the paper table. Arms are emitted in sorted
label order so the rendered report is deterministic regardless of dict insertion
order.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from fallow_bench.analysis import latency, recovery, throughput, yield_metrics
from fallow_bench.analysis.energy import marginal_energy_per_1k_tokens
from fallow_bench.analysis.models import (
    AnalysisConfig,
    MetricRow,
    MetricTable,
    RunFrames,
)

# Each entry: (row label, function RunFrames+config -> value). Order is the table.
_METRICS: tuple[tuple[str, Callable[[RunFrames, AnalysisConfig], float | None]], ...] = (
    ("TTFT p50 (s)", lambda r, _c: latency.ttft_p50(r.client)),
    ("TTFT p95 (s)", lambda r, _c: latency.ttft_p95(r.client)),
    ("Decode tok/s p50", lambda r, _c: latency.decode_tps_p50(r.client)),
    ("Batch units/hour", lambda r, _c: throughput.units_per_hour(r.jobs)),
    ("Failure-recovery (s)", lambda r, _c: recovery.failure_recovery_s(r.churn, r.jobs)),
    ("Time-to-yield p50 (ms)", lambda r, _c: yield_metrics.yield_p50(r.events, r.churn)),
    ("Time-to-yield p99 (ms)", lambda r, _c: yield_metrics.yield_p99(r.events, r.churn)),
    (
        "Marginal energy per 1k tokens (J)",
        lambda r, c: marginal_energy_per_1k_tokens(r.power, r.client, c.energy_baseline),
    ),
    ("% served on-prem", lambda r, _c: throughput.pct_served_on_prem(r.gateway)),
)


def build_table(runs: Mapping[str, RunFrames], config: AnalysisConfig) -> MetricTable:
    """Compute every metric for every arm (sorted by label)."""
    arms = tuple(sorted(runs))
    rows = tuple(
        MetricRow(label=label, values=tuple(fn(runs[arm], config) for arm in arms))
        for label, fn in _METRICS
    )
    return MetricTable(arms=arms, rows=rows)
