"""Unit tests for ReclaimController — the sticky user-triggered takedown."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from preempt_fakes import FakeClock, RecordingSupervisor, suspended

from fallow_agent.preempt import ReclaimController, request_reclaim, request_release
from fallow_agent.preempt.reclaim import reclaim_control_path

# The preemption latency budget the suspend must clear (see the issue: p99 well
# under 300ms). The suspend is the same hot-path call automatic preemption uses.
_BUDGET_MS = 300.0


def _sync_runner() -> Callable[[Callable[[], None]], None]:
    """A stop-runner that runs the stop step inline, so order is observable."""
    return lambda fn: fn()


def _make(
    flag: Path,
    *,
    statuses: tuple = (),
    clock: FakeClock | None = None,
) -> tuple[ReclaimController, list[str], RecordingSupervisor]:
    log: list[str] = []
    supervisor = RecordingSupervisor(log, statuses)
    controller = ReclaimController(
        supervisor,
        flag,
        monotonic=clock if clock is not None else time.monotonic,
        stop_runner=_sync_runner(),
    )
    return controller, log, supervisor


def test_reclaim_suspends_immediately_then_stops(tmp_path: Path) -> None:
    flag = tmp_path / "reclaim.flag"
    statuses = (suspended("m1", 8100), suspended("m2", 8101))
    controller, log, _supervisor = _make(flag, statuses=statuses)

    # Idle: no flag, nothing happens.
    assert controller.on_poll(0.0) is False
    assert log == []

    # User reclaims: flag appears -> suspend-all first, then stop every replica.
    flag.touch()
    assert controller.on_poll(0.0) is True
    assert controller.is_reclaimed() is True
    assert log[0] == "suspend_all"
    assert log.index("suspend_all") < log.index("stop_replica:m1")
    assert log[1:] == ["stop_replica:m1", "stop_replica:m2"]


def test_reclaim_is_sticky_until_release(tmp_path: Path) -> None:
    flag = tmp_path / "reclaim.flag"
    controller, log, _supervisor = _make(flag)
    flag.touch()
    controller.on_poll(0.0)
    log.clear()

    # Still reclaimed on later ticks; the transition work does not repeat.
    assert controller.on_poll(0.0) is True
    assert log == []

    # Release: flag removed -> normal serving restored on the next tick.
    flag.unlink()
    assert controller.on_poll(0.0) is False
    assert controller.is_reclaimed() is False


def test_reclaim_suspend_clears_the_latency_budget(tmp_path: Path) -> None:
    flag = tmp_path / "reclaim.flag"
    controller, log, _supervisor = _make(flag)  # real monotonic clock

    flag.touch()
    start = time.monotonic()
    assert controller.on_poll(start) is True
    elapsed_ms = (time.monotonic() - start) * 1000.0

    assert log[0] == "suspend_all"  # suspend is the first observable side effect
    assert elapsed_ms < _BUDGET_MS


def test_request_reclaim_and_release_toggle_the_flag(tmp_path: Path) -> None:
    state_path = tmp_path / "state" / "agent-state.json"

    path = request_reclaim(state_path)
    assert path == reclaim_control_path(state_path)
    assert path.exists()

    request_release(state_path)
    assert not path.exists()
    # Releasing when already released is a no-op, not an error.
    request_release(state_path)
