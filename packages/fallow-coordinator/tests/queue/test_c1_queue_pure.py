"""Pure-logic tests for the C1 helpers (no I/O)."""

from datetime import UTC, datetime

from fallow_coordinator.queue._jobstate import UnitCounts, next_job_state
from fallow_coordinator.queue._serialization import (
    dump_metrics,
    dump_params,
    lease_expiry,
    lease_window_s,
    to_iso,
)
from fallow_coordinator.queue._sql import JOB_DONE, JOB_PENDING, JOB_RUNNING
from fallow_protocol.messages import WorkMetrics


def test_next_job_state_terminal_when_no_runnable_units() -> None:
    assert next_job_state(UnitCounts(0, 0, 0, 0, 0)) == JOB_DONE
    assert next_job_state(UnitCounts(2, 0, 0, 1, 1)) == JOB_DONE


def test_next_job_state_running_and_pending() -> None:
    assert next_job_state(UnitCounts(2, 2, 0, 0, 0)) == JOB_PENDING
    assert next_job_state(UnitCounts(2, 1, 1, 0, 0)) == JOB_RUNNING
    assert next_job_state(UnitCounts(2, 1, 0, 1, 0)) == JOB_RUNNING


def test_lease_window_uses_larger_of_estimate_and_floor() -> None:
    assert lease_window_s(None, 120.0) == 120.0
    assert lease_window_s(10.0, 120.0) == 120.0  # 2*10 < 120
    assert lease_window_s(100.0, 120.0) == 200.0  # 2*100 > 120


def test_lease_expiry_is_now_plus_window() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    expiry = lease_expiry(now, 100.0, 120.0)
    assert (expiry - now).total_seconds() == 200.0


def test_to_iso_is_fixed_width_and_sorts_chronologically() -> None:
    earlier = to_iso(datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC))
    later = to_iso(datetime(2026, 1, 1, 0, 0, 0, 500_000, tzinfo=UTC))
    assert earlier < later  # lexicographic == chronological
    assert earlier.endswith("+00:00") and "." in earlier


def test_dump_params_is_deterministic() -> None:
    assert dump_params({"b": "2", "a": "1"}) == dump_params({"a": "1", "b": "2"})


def test_dump_metrics_optional() -> None:
    assert dump_metrics(None) is None
    assert "duration_s" in (dump_metrics(WorkMetrics(duration_s=1.0)) or "")
