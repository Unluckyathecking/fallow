"""Committed work-unit transition notifications from the SQLite queue."""

from pathlib import Path

from queue_helpers import (
    LEASE_FLOOR_S,
    FakeClock,
    complete_succeeded,
    make_units,
    submit,
    succeeded,
)

from fallow_coordinator.queue import SqliteQueueStore
from fallow_protocol.messages import UnitTransition, WorkUnitState


async def test_successful_lease_emits_committed_transition(
    tmp_path: Path, clock: FakeClock
) -> None:
    transitions: list[UnitTransition] = []
    store = SqliteQueueStore(tmp_path / "queue.db", now=clock, on_transition=transitions.append)
    await store.init()
    try:
        job_id = await submit(store, make_units(1))

        lease = await store.lease_next("agent-a", ["m1"])

        assert lease is not None
        assert transitions == [
            UnitTransition(
                work_unit_id="u0",
                job_id=job_id,
                agent_id="agent-a",
                attempt=1,
                state=WorkUnitState.LEASED,
                at=clock(),
            )
        ]
    finally:
        await store.close()


async def test_accepted_completion_emits_done_transition(tmp_path: Path, clock: FakeClock) -> None:
    transitions: list[UnitTransition] = []
    store = SqliteQueueStore(tmp_path / "queue.db", now=clock, on_transition=transitions.append)
    await store.init()
    try:
        job_id = await submit(store, make_units(1))
        lease = await store.lease_next("agent-a", ["m1"])
        assert lease is not None
        transitions.clear()

        await complete_succeeded(store, "agent-a", lease)

        assert transitions == [
            UnitTransition(
                work_unit_id="u0",
                job_id=job_id,
                agent_id="agent-a",
                attempt=1,
                state=WorkUnitState.DONE,
                at=clock(),
            )
        ]
    finally:
        await store.close()


async def test_retryable_requeue_emits_pending_transition(tmp_path: Path, clock: FakeClock) -> None:
    transitions: list[UnitTransition] = []
    store = SqliteQueueStore(
        tmp_path / "queue.db",
        now=clock,
        max_attempts=2,
        default_lease_s=LEASE_FLOOR_S,
        on_transition=transitions.append,
    )
    await store.init()
    try:
        job_id = await submit(store, make_units(1))
        assert await store.lease_next("agent-a", ["m1"]) is not None
        transitions.clear()
        clock.advance(61)

        assert await store.requeue_expired() == 1

        assert transitions == [
            UnitTransition(
                work_unit_id="u0",
                job_id=job_id,
                agent_id="agent-a",
                attempt=1,
                state=WorkUnitState.PENDING,
                at=clock(),
            )
        ]
    finally:
        await store.close()


async def test_exhausted_requeue_emits_dead_transition(tmp_path: Path, clock: FakeClock) -> None:
    transitions: list[UnitTransition] = []
    store = SqliteQueueStore(
        tmp_path / "queue.db",
        now=clock,
        max_attempts=1,
        default_lease_s=LEASE_FLOOR_S,
        on_transition=transitions.append,
    )
    await store.init()
    try:
        job_id = await submit(store, make_units(1))
        assert await store.lease_next("agent-a", ["m1"]) is not None
        transitions.clear()
        clock.advance(21)

        assert await store.requeue_expired() == 1

        assert transitions == [
            UnitTransition(
                work_unit_id="u0",
                job_id=job_id,
                agent_id="agent-a",
                attempt=1,
                state=WorkUnitState.DEAD,
                at=clock(),
            )
        ]
    finally:
        await store.close()


async def test_noop_late_and_duplicate_operations_emit_nothing(
    tmp_path: Path, clock: FakeClock
) -> None:
    transitions: list[UnitTransition] = []
    store = SqliteQueueStore(
        tmp_path / "queue.db",
        now=clock,
        default_lease_s=LEASE_FLOOR_S,
        on_transition=transitions.append,
    )
    await store.init()
    try:
        await submit(store, make_units(1))
        first = await store.lease_next("agent-a", ["m1"])
        assert first is not None
        clock.advance(21)
        assert await store.requeue_expired() == 1
        second = await store.lease_next("agent-b", ["m1"])
        assert second is not None
        transitions.clear()

        assert await store.lease_next("agent-c", ["m1"]) is None
        await store.complete_unit("agent-a", first.attempt, succeeded("u0"))
        assert await store.requeue_agent("agent-c") == 0
        assert transitions == []

        await complete_succeeded(store, "agent-b", second)
        transitions.clear()
        await store.complete_unit("agent-b", second.attempt, succeeded("u0"))
        assert transitions == []
    finally:
        await store.close()


async def test_observer_failure_does_not_fail_or_rollback_queue_operation(
    tmp_path: Path, clock: FakeClock
) -> None:
    observed: list[UnitTransition] = []

    def failing_observer(transition: UnitTransition) -> None:
        observed.append(transition)
        raise RuntimeError("observer unavailable")

    store = SqliteQueueStore(tmp_path / "queue.db", now=clock, on_transition=failing_observer)
    await store.init()
    try:
        await submit(store, make_units(2))

        first = await store.lease_next("agent-a", ["m1"])
        second = await store.lease_next("agent-b", ["m1"])

        assert first is not None and first.work_unit_id == "u0"
        assert second is not None and second.work_unit_id == "u1"
        assert [transition.work_unit_id for transition in observed] == ["u0", "u1"]
    finally:
        await store.close()
