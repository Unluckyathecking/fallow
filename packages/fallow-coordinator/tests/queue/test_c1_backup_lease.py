"""Speculative backup lease mechanism (module C1, ADR 056).

The queue's job here: grant a bounded second lease on an in-flight unit, let the
first completion win idempotently, and keep counts honest when two agents finish
the same unit. The survival *decision* lives in the scheduler and is tested
separately; these tests drive the store primitive directly with an injected clock.
"""

from __future__ import annotations

from queue_helpers import (
    LEASE_FLOOR_S,
    FakeClock,
    make_units,
    submit,
    succeeded,
)

from fallow_coordinator.queue import SqliteQueueStore
from fallow_protocol.messages import (
    JobState,
    WorkMetrics,
    WorkResult,
    WorkResultStatus,
    WorkUnitLease,
)

BACKUP_REF = "result://backup/u0"
BACKUP_DIGEST = "b" * 64


def _backup_success(work_unit_id: str, result_ref: str = BACKUP_REF) -> WorkResult:
    return WorkResult(
        work_unit_id=work_unit_id,
        status=WorkResultStatus.SUCCEEDED,
        result_ref=result_ref,
        metrics=WorkMetrics(duration_s=2.0, items=3),
    )


async def _lease_one(store: SqliteQueueStore, agent_id: str) -> WorkUnitLease:
    lease = await store.lease_next(agent_id, ["m1"])
    assert lease is not None
    return lease


async def _backup_complete(
    store: SqliteQueueStore, agent_id: str, backup: WorkUnitLease, result_ref: str = BACKUP_REF
) -> bool:
    assert await store.bind_result_payload(
        agent_id, backup.work_unit_id, backup.attempt, digest=BACKUP_DIGEST, result_ref=result_ref
    )
    return await store.complete_unit(
        agent_id, backup.attempt, _backup_success(backup.work_unit_id, result_ref)
    )


# ── the second lease is bounded ───────────────────────────────────────────────


async def test_backup_lease_grants_a_second_lease_on_a_held_unit(store: SqliteQueueStore) -> None:
    await submit(store, make_units(1))
    primary = await _lease_one(store, "holder")

    backup = await store.lease_backup("helper", primary.work_unit_id)

    assert backup is not None
    assert backup.work_unit_id == primary.work_unit_id
    # A reserved attempt one past the retry budget (max_attempts=4) so its payload
    # binding can never collide with a primary attempt for the same unit.
    assert backup.attempt == 5
    assert backup.model_id == primary.model_id


async def test_at_most_one_backup_per_unit(store: SqliteQueueStore) -> None:
    await submit(store, make_units(1))
    primary = await _lease_one(store, "holder")

    first = await store.lease_backup("helper-a", primary.work_unit_id)
    second = await store.lease_backup("helper-b", primary.work_unit_id)

    assert first is not None
    assert second is None  # the unit already has its one backup


async def test_backup_refused_for_pending_own_or_missing_units(store: SqliteQueueStore) -> None:
    await submit(store, make_units(1))

    # Pending (not yet leased): nothing to back up.
    assert await store.lease_backup("helper", "u0") is None

    primary = await _lease_one(store, "holder")
    # An agent cannot back up a unit it already holds.
    assert await store.lease_backup("holder", primary.work_unit_id) is None
    # Unknown unit id.
    assert await store.lease_backup("helper", "does-not-exist") is None


# ── first completion wins; the second is an idempotent no-op ──────────────────


async def test_double_complete_finalizes_once_and_counts_stay_honest(
    store: SqliteQueueStore,
) -> None:
    job_id = await submit(store, make_units(1))
    primary = await _lease_one(store, "holder")
    backup = await store.lease_backup("helper", primary.work_unit_id)
    assert backup is not None
    unit_id = primary.work_unit_id

    # Both agents upload their payloads while the unit is still in flight.
    assert await store.bind_result_payload(
        "holder", unit_id, primary.attempt, digest="a" * 64, result_ref="result://u0"
    )
    assert await store.bind_result_payload(
        "helper", unit_id, backup.attempt, digest=BACKUP_DIGEST, result_ref=BACKUP_REF
    )
    # The primary completes first and finalizes the unit.
    assert await store.complete_unit("holder", primary.attempt, succeeded(unit_id)) is True
    # The backup then completes the same unit: a clean, idempotent no-op.
    assert await store.complete_unit("helper", backup.attempt, _backup_success(unit_id)) is True

    status = await store.job_status(job_id)
    assert status is not None
    assert status.state is JobState.DONE
    assert status.total_units == 1
    assert status.done_units == 1  # finalized exactly once, not double-counted
    # The winner's payload (the primary's) is the one retrievable.
    assert await store.completed_result_ref(unit_id) == "result://u0"


async def test_backup_win_finalizes_and_its_payload_is_retrievable(
    store: SqliteQueueStore,
) -> None:
    job_id = await submit(store, make_units(1))
    primary = await _lease_one(store, "holder")
    backup = await store.lease_backup("helper", primary.work_unit_id)
    assert backup is not None

    # The holder churns and never completes; the backup finishes first.
    won = await _backup_complete(store, "helper", backup)

    assert won is True
    status = await store.job_status(job_id)
    assert status is not None
    assert status.state is JobState.DONE
    assert status.done_units == 1
    # Downstream ref lookup finds the backup's own binding, not the primary's.
    assert await store.completed_result_ref(primary.work_unit_id) == BACKUP_REF
    # The stranded primary can no longer clobber the finished unit: its late
    # completion is cleanly rejected, leaving the backup's result in place.
    assert (
        await store.complete_unit("holder", primary.attempt, succeeded(primary.work_unit_id))
        is False
    )
    assert await store.completed_result_ref(primary.work_unit_id) == BACKUP_REF


async def test_backup_wins_before_lease_expiry_would_requeue(
    store: SqliteQueueStore, clock: FakeClock
) -> None:
    # The whole point: the backup finishes the unit inside the primary's lease
    # window, so the job is DONE without ever waiting for expiry + requeue.
    job_id = await submit(store, make_units(1))
    primary = await _lease_one(store, "holder")
    backup = await store.lease_backup("helper", primary.work_unit_id)
    assert backup is not None

    clock.advance(LEASE_FLOOR_S - 1.0)  # still inside the primary's lease
    assert await _backup_complete(store, "helper", backup) is True

    status = await store.job_status(job_id)
    assert status is not None
    assert status.state is JobState.DONE

    # Even past the primary's expiry, there is nothing left to requeue: the unit
    # is already done, so lease-expiry never gets a chance to add its dead time.
    clock.advance(10.0)
    assert await store.requeue_expired() == 0


async def test_backup_completion_after_primary_expiry_salvages_the_unit(
    store: SqliteQueueStore, clock: FakeClock
) -> None:
    # Holder churns; its lease expires and the unit requeues to pending. A backup
    # still in flight may finalize it, beating a fresh re-lease + re-run.
    job_id = await submit(store, make_units(1))
    primary = await _lease_one(store, "holder")
    backup = await store.lease_backup("helper", primary.work_unit_id)
    assert backup is not None

    clock.advance(LEASE_FLOOR_S + 1.0)
    assert await store.requeue_expired() == 1  # primary's lease expired → pending

    assert await _backup_complete(store, "helper", backup) is True
    status = await store.job_status(job_id)
    assert status is not None
    assert status.state is JobState.DONE
    assert status.done_units == 1


# ── the primary lane is untouched when no backup exists ───────────────────────


async def test_no_backup_leaves_upload_and_completion_unchanged(store: SqliteQueueStore) -> None:
    await submit(store, make_units(1))
    primary = await _lease_one(store, "holder")

    # A non-holder with no backup lease has no upload attempt and cannot complete.
    assert await store.result_upload_attempt("stranger", primary.work_unit_id) is None
    stray = _backup_success(primary.work_unit_id, result_ref="result://stray")
    assert await store.complete_unit("stranger", 1, stray) is False
