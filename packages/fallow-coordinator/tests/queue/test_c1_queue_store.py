"""Behavioural tests for the C1 SQLite queue store."""

import asyncio
from datetime import timedelta

from conftest import (
    LEASE_FLOOR_S,
    FakeClock,
    make_job,
    make_units,
    submit,
    succeeded,
)

from fallow_coordinator.queue import SqliteQueueStore
from fallow_protocol.capabilities import WorkerKind
from fallow_protocol.messages import (
    JobState,
    WorkResult,
    WorkResultStatus,
    WorkUnitState,
)


async def test_submit_lease_complete_happy_path(store: SqliteQueueStore, clock: FakeClock) -> None:
    job_id = await submit(store, make_units(1))

    status = await store.job_status(job_id)
    assert status is not None
    assert status.state is JobState.PENDING
    assert status.total_units == 1

    lease = await store.lease_next("agent-a", ["m1"])
    assert lease is not None
    assert lease.job_id == job_id
    assert lease.work_unit_id == "u0"
    assert lease.kind is WorkerKind.EMBED
    assert lease.model_id == "m1"
    assert lease.input_url == "input://u0"  # passthrough of input_ref
    assert lease.attempt == 1
    assert lease.lease_expires == clock() + timedelta(seconds=LEASE_FLOOR_S)

    running = await store.job_status(job_id)
    assert running is not None and running.state is JobState.RUNNING

    await store.complete_unit("agent-a", succeeded("u0"))

    done = await store.job_status(job_id)
    assert done is not None
    assert done.state is JobState.DONE
    assert done.done_units == 1
    assert done.dead_units == 0


async def test_lease_window_scales_with_estimate(store: SqliteQueueStore, clock: FakeClock) -> None:
    # est=100 → 2*100 = 200s dominates the 20s floor.
    await store.submit_job(make_job(), make_units(1, est=100.0))
    lease = await store.lease_next("agent-a", ["m1"])
    assert lease is not None
    assert lease.lease_expires == clock() + timedelta(seconds=200)


async def test_lease_next_empty_when_no_pending(store: SqliteQueueStore) -> None:
    await submit(store, make_units(1))
    await store.lease_next("agent-a", ["m1"])
    assert await store.lease_next("agent-b", ["m1"]) is None


async def test_duplicate_complete_is_noop(store: SqliteQueueStore) -> None:
    job_id = await submit(store, make_units(1))
    await store.lease_next("agent-a", ["m1"])
    await store.complete_unit("agent-a", succeeded("u0"))

    # A second completion (even a FAILED one) must not change anything.
    await store.complete_unit(
        "agent-a",
        WorkResult(work_unit_id="u0", status=WorkResultStatus.FAILED, error="late"),
    )
    status = await store.job_status(job_id)
    assert status is not None
    assert status.state is JobState.DONE
    assert status.done_units == 1


async def test_late_completion_after_reassignment_does_not_clobber(
    store: SqliteQueueStore, clock: FakeClock
) -> None:
    job_id = await submit(store, make_units(1))
    first = await store.lease_next("agent-a", ["m1"])
    assert first is not None and first.attempt == 1

    # agent-a's lease expires; requeue makes the unit pending again.
    clock.advance(21)
    assert await store.requeue_expired() == 1

    # agent-b picks it up with a fresh, valid lease.
    second = await store.lease_next("agent-b", ["m1"])
    assert second is not None and second.attempt == 2

    # agent-a returns late while agent-b holds a valid lease → rejected no-op.
    await store.complete_unit("agent-a", succeeded("u0"))
    mid = await store.job_status(job_id)
    assert mid is not None and mid.state is JobState.RUNNING and mid.done_units == 0

    # agent-b's result wins.
    await store.complete_unit("agent-b", succeeded("u0"))
    final = await store.job_status(job_id)
    assert final is not None and final.state is JobState.DONE and final.done_units == 1


async def test_completion_accepted_for_expired_lease_same_agent(
    store: SqliteQueueStore, clock: FakeClock
) -> None:
    await submit(store, make_units(1))
    lease = await store.lease_next("agent-a", ["m1"])
    assert lease is not None
    clock.advance(21)  # lease now expired but not yet requeued/reassigned
    await store.complete_unit("agent-a", succeeded("u0"))
    status = await store.job_status(await _only_job(store))
    assert status is not None and status.done_units == 1


async def test_requeue_expired_increments_attempts_and_deads_at_max(
    tmp_path_factory, clock: FakeClock
) -> None:
    from pathlib import Path

    db: Path = tmp_path_factory.mktemp("q") / "queue.db"
    store = SqliteQueueStore(db, now=clock, max_attempts=2, default_lease_s=LEASE_FLOOR_S)
    await store.init()
    try:
        job_id = await submit(store, make_units(1))

        lease1 = await store.lease_next("agent-a", ["m1"])
        assert lease1 is not None and lease1.attempt == 1
        clock.advance(21)
        assert await store.requeue_expired() == 1  # attempts 1 < 2 → pending

        lease2 = await store.lease_next("agent-a", ["m1"])
        assert lease2 is not None and lease2.attempt == 2
        clock.advance(21)
        assert await store.requeue_expired() == 1  # attempts 2 >= 2 → dead

        status = await store.job_status(job_id)
        assert status is not None
        assert status.state is JobState.DONE  # all units terminal (dead)
        assert status.dead_units == 1
        assert status.done_units == 0

        # Nothing left to requeue or lease.
        assert await store.requeue_expired() == 0
        assert await store.lease_next("agent-a", ["m1"]) is None
    finally:
        await store.close()


async def test_dedup_on_resubmit_completes_instantly(
    store: SqliteQueueStore, clock: FakeClock
) -> None:
    units = make_units(2)
    job1 = await submit(store, units)
    for _ in range(2):
        lease = await store.lease_next("agent-a", ["m1"])
        assert lease is not None
        await store.complete_unit("agent-a", succeeded(lease.work_unit_id))
    assert (await store.job_status(job1)).state is JobState.DONE  # type: ignore[union-attr]

    # Resubmitting the same content-addressed units completes with zero leasing.
    job2 = await submit(store, units)
    status = await store.job_status(job2)
    assert status is not None
    assert status.state is JobState.DONE
    assert status.done_units == 2
    assert await store.lease_next("agent-a", ["m1"]) is None


async def test_partial_dedup_leaves_unfinished_unit_pending(
    store: SqliteQueueStore,
) -> None:
    units = make_units(2)
    await submit(store, units)
    lease = await store.lease_next("agent-a", ["m1"])
    assert lease is not None
    await store.complete_unit("agent-a", succeeded(lease.work_unit_id))

    # Only one unit has a stored result; resubmit → one DONE, one PENDING.
    job2 = await submit(store, units)
    status = await store.job_status(job2)
    assert status is not None
    assert status.state is JobState.RUNNING
    assert status.done_units == 1
    lease2 = await store.lease_next("agent-b", ["m1"])
    assert lease2 is not None and lease2.work_unit_id != lease.work_unit_id


async def test_lease_next_respects_model_filter(store: SqliteQueueStore) -> None:
    await submit(store, make_units(1), model_id="m1")
    assert await store.lease_next("agent-a", ["m2"]) is None
    assert await store.lease_next("agent-a", []) is None
    lease = await store.lease_next("agent-a", ["m2", "m1"])
    assert lease is not None and lease.model_id == "m1"


async def test_lease_next_priority_then_idx_order(store: SqliteQueueStore) -> None:
    await submit(store, make_units(2, prefix="lo"), priority=0)
    await submit(store, make_units(2, prefix="hi"), priority=5)

    order = []
    for _ in range(4):
        lease = await store.lease_next("agent-a", ["m1"])
        assert lease is not None
        order.append(lease.work_unit_id)
    # High-priority job first (idx order), then low-priority job (idx order).
    assert order == ["hi0", "hi1", "lo0", "lo1"]


async def test_concurrent_lease_never_double_leases(store: SqliteQueueStore) -> None:
    await submit(store, make_units(5))

    results = await asyncio.gather(*(store.lease_next(f"agent-{i}", ["m1"]) for i in range(10)))
    leases = [r for r in results if r is not None]
    ids = [lease.work_unit_id for lease in leases]

    assert len(leases) == 5  # exactly the five available units
    assert len(set(ids)) == 5  # each leased at most once
    assert sorted(ids) == ["u0", "u1", "u2", "u3", "u4"]


async def test_requeue_agent_evicts_offline_agent(
    store: SqliteQueueStore, clock: FakeClock
) -> None:
    await submit(store, make_units(2))
    a = await store.lease_next("agent-a", ["m1"])
    b = await store.lease_next("agent-b", ["m1"])
    assert a is not None and b is not None

    # agent-a goes offline; its unit is requeued, agent-b's is untouched.
    assert await store.requeue_agent("agent-a") == 1
    reclaimed = await store.lease_next("agent-c", ["m1"])
    assert reclaimed is not None and reclaimed.work_unit_id == a.work_unit_id
    assert reclaimed.attempt == 2


async def test_extend_leases_pushes_expiry_out(store: SqliteQueueStore, clock: FakeClock) -> None:
    await submit(store, make_units(1))
    lease = await store.lease_next("agent-a", ["m1"])
    assert lease is not None

    clock.advance(15)  # not yet expired (window is 20s)
    await store.extend_leases("agent-a", ["u0"])

    clock.advance(15)  # 30s since lease; original 20s window would have expired
    assert await store.requeue_expired() == 0  # extension held

    clock.advance(200)
    assert await store.requeue_expired() == 1  # finally expired


async def test_job_status_none_for_unknown(store: SqliteQueueStore) -> None:
    assert await store.job_status("does-not-exist") is None


async def test_empty_job_is_done(store: SqliteQueueStore) -> None:
    job_id = await submit(store, [])
    status = await store.job_status(job_id)
    assert status is not None
    assert status.state is JobState.DONE
    assert status.total_units == 0


async def _only_job(store: SqliteQueueStore) -> str:
    # Helper: recover the single job id via a lease-free status probe path.
    # Tests that need the id capture it directly; this is only used where a
    # single job exists and its id was not retained.
    cursor = await store._db.execute("SELECT job_id FROM jobs LIMIT 1")
    row = await cursor.fetchone()
    assert row is not None
    return str(row["job_id"])


def test_work_unit_state_values_are_stable() -> None:
    # Guards the on-disk enum contract the SQL depends on.
    assert WorkUnitState.PENDING.value == "pending"
    assert WorkUnitState.LEASED.value == "leased"
    assert WorkUnitState.DONE.value == "done"
    assert WorkUnitState.DEAD.value == "dead"
