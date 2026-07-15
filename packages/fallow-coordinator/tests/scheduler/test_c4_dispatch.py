"""Tests for DispatchLoop — the impure sweep loop that never dies on errors."""

from collections.abc import Sequence
from datetime import UTC, datetime

import pytest
from scheduler_helpers import FakeQueue

from fallow_coordinator.scheduler import CapabilityScheduler, DispatchLoop
from fallow_protocol.messages import AgentSnapshot

FIXED_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def fixed_clock() -> datetime:
    return FIXED_NOW


async def empty_snapshots() -> Sequence[AgentSnapshot]:
    return ()


def make_loop(queue: FakeQueue, sleep: object = None) -> DispatchLoop:
    kwargs = {"sleep": sleep} if sleep is not None else {}
    return DispatchLoop(
        queue=queue,
        snapshots=empty_snapshots,
        policy=CapabilityScheduler(),
        requeue_interval_s=10.0,
        now=fixed_clock,
        **kwargs,  # type: ignore[arg-type]
    )


async def test_tick_calls_requeue_expired_and_reports_count() -> None:
    queue = FakeQueue(requeue_return=3)
    loop = make_loop(queue)
    stats = await loop.tick()
    assert queue.requeue_calls == 1
    assert stats.requeued == 3
    assert stats.error is None
    assert stats.at == FIXED_NOW


async def test_tick_survives_store_exceptions() -> None:
    queue = FakeQueue(requeue_error=RuntimeError("db is on fire"))
    loop = make_loop(queue)
    stats = await loop.tick()  # must not raise
    assert queue.requeue_calls == 1
    assert stats.requeued == 0
    assert "db is on fire" in (stats.error or "")


async def test_run_forever_ticks_until_stopped() -> None:
    queue = FakeQueue(requeue_return=1)
    intervals: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        intervals.append(seconds)
        if len(intervals) >= 3:
            loop.stop()

    loop = make_loop(queue, sleep=fake_sleep)
    await loop.run_forever()
    # Loop body ran once per sleep; it stopped after the 3rd sleep.
    assert queue.requeue_calls == 3
    assert intervals == [10.0, 10.0, 10.0]


async def test_run_forever_keeps_going_through_store_errors() -> None:
    queue = FakeQueue(requeue_error=RuntimeError("boom"))
    ticks = 0

    async def fake_sleep(_seconds: float) -> None:
        nonlocal ticks
        ticks += 1
        if ticks >= 2:
            loop.stop()

    loop = make_loop(queue, sleep=fake_sleep)
    await loop.run_forever()  # errors are swallowed by tick(); loop survives
    assert queue.requeue_calls == 2


def test_constructor_rejects_non_positive_interval() -> None:
    queue = FakeQueue()
    with pytest.raises(ValueError, match="requeue_interval_s"):
        DispatchLoop(
            queue=queue,
            snapshots=empty_snapshots,
            policy=CapabilityScheduler(),
            requeue_interval_s=0.0,
            now=fixed_clock,
        )


def test_loop_exposes_policy_and_snapshots_for_app_layer() -> None:
    queue = FakeQueue()
    policy = CapabilityScheduler()
    loop = DispatchLoop(
        queue=queue,
        snapshots=empty_snapshots,
        policy=policy,
        requeue_interval_s=5.0,
        now=fixed_clock,
    )
    assert loop.policy is policy
    assert loop.snapshots is empty_snapshots
