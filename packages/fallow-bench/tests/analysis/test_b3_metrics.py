"""Every headline metric against hand-computed values on the run_a fixture."""

from __future__ import annotations

import pytest
from b3_helpers import RUN_A, config

from fallow_bench.analysis import latency, recovery, throughput, yield_metrics
from fallow_bench.analysis.loaders import load_run

_FRAMES = load_run(RUN_A, config())


def test_ttft_percentiles() -> None:
    # TTFT samples sorted = [0.1, 0.2, 0.3]; linear p50=0.2, p95=0.29.
    assert latency.ttft_p50(_FRAMES.client) == pytest.approx(0.2)
    assert latency.ttft_p95(_FRAMES.client) == pytest.approx(0.29)


def test_decode_tps_p50() -> None:
    # per-request tok/s = [100, 200, 300]; median 200.
    assert latency.decode_tps_p50(_FRAMES.client) == pytest.approx(200.0)


def test_units_per_hour() -> None:
    # 2 done units over an 18 s span -> 2 / (18/3600) = 400.
    assert throughput.units_per_hour(_FRAMES.jobs) == pytest.approx(400.0)


def test_pct_served_on_prem() -> None:
    # 8 served of 10 -> 80 %.
    assert throughput.pct_served_on_prem(_FRAMES.gateway) == pytest.approx(80.0)


def test_failure_recovery() -> None:
    # u1 leased on killed A (kill@5), done on B@20 -> 15 s.
    assert recovery.failure_recovery_s(_FRAMES.churn, _FRAMES.jobs) == pytest.approx(15.0)


def test_time_to_yield() -> None:
    # yield samples = events[1,3] + churn flips[2,4] = [1,2,3,4]; p50=2.5, p99=3.97.
    assert yield_metrics.yield_p50(_FRAMES.events, _FRAMES.churn) == pytest.approx(2.5)
    assert yield_metrics.yield_p99(_FRAMES.events, _FRAMES.churn) == pytest.approx(3.97)


def test_empty_frames_return_none() -> None:
    from pathlib import Path

    from b3_helpers import config as _config

    from fallow_bench.analysis.loaders import load_run as _load

    empty = _load(Path("/nope"), _config())
    assert latency.ttft_p50(empty.client) is None
    assert throughput.units_per_hour(empty.jobs) is None
    assert throughput.pct_served_on_prem(empty.gateway) is None
    assert recovery.failure_recovery_s(empty.churn, empty.jobs) is None
    assert yield_metrics.yield_p50(empty.events, empty.churn) is None
