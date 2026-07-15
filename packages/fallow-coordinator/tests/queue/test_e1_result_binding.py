"""Lease-safe result-payload bindings for E1.2."""

import asyncio

from queue_helpers import make_units, submit, succeeded

from fallow_coordinator.queue import SqliteQueueStore
from fallow_protocol.messages import JobState, WorkResult, WorkResultStatus


async def test_result_upload_attempt_requires_current_lease_holder(
    store: SqliteQueueStore,
) -> None:
    await submit(store, make_units(1))
    lease = await store.lease_next("agent-a", ["m1"])
    assert lease is not None

    assert await store.result_upload_attempt("agent-a", "u0") == lease.attempt
    assert await store.result_upload_attempt("agent-b", "u0") is None
    assert await store.result_upload_attempt("agent-a", "missing") is None


async def test_bind_result_payload_accepts_matching_active_attempt(
    store: SqliteQueueStore,
) -> None:
    await submit(store, make_units(1))
    lease = await store.lease_next("agent-a", ["m1"])
    assert lease is not None

    accepted = await store.bind_result_payload(
        "agent-a",
        "u0",
        lease.attempt,
        digest="a" * 64,
        result_ref="sha256:" + "a" * 64,
    )

    assert accepted is True

    duplicate, conflicting = await asyncio.gather(
        store.bind_result_payload("agent-a", "u0", lease.attempt, "a" * 64, "sha256:" + "a" * 64),
        store.bind_result_payload("agent-a", "u0", lease.attempt, "b" * 64, "sha256:" + "b" * 64),
    )
    assert duplicate is True
    assert conflicting is False


async def test_bind_result_payload_rechecks_lease_after_stream(
    store: SqliteQueueStore,
) -> None:
    await submit(store, make_units(1))
    first = await store.lease_next("agent-a", ["m1"])
    assert first is not None
    assert await store.bind_result_payload(
        "agent-a", "u0", first.attempt, "a" * 64, "sha256:" + "a" * 64
    )

    assert await store.requeue_agent("agent-a") == 1
    second = await store.lease_next("agent-b", ["m1"])
    assert second is not None and second.attempt == first.attempt + 1

    assert not await store.bind_result_payload(
        "agent-a", "u0", first.attempt, "a" * 64, "sha256:" + "a" * 64
    )


async def test_successful_completion_requires_attempt_binding(
    store: SqliteQueueStore,
) -> None:
    job_id = await submit(store, make_units(1))
    lease = await store.lease_next("agent-a", ["m1"])
    assert lease is not None

    accepted = await store.complete_unit("agent-a", lease.attempt, succeeded("u0"))

    assert accepted is False
    status = await store.job_status(job_id)
    assert status is not None
    assert status.state is JobState.RUNNING


async def test_completed_result_ref_returns_only_accepted_success(
    store: SqliteQueueStore,
) -> None:
    await submit(store, make_units(1))
    lease = await store.lease_next("agent-a", ["m1"])
    assert lease is not None
    result = succeeded("u0")
    assert result.result_ref is not None
    assert await store.bind_result_payload(
        "agent-a", "u0", lease.attempt, "b" * 64, result.result_ref
    )

    assert await store.completed_result_ref("u0") is None
    await store.complete_unit("agent-a", lease.attempt, result)

    assert await store.completed_result_ref("u0") == result.result_ref


async def test_failed_completion_needs_no_payload_binding(store: SqliteQueueStore) -> None:
    job_id = await submit(store, make_units(1))
    lease = await store.lease_next("agent-a", ["m1"])
    assert lease is not None

    await store.complete_unit(
        "agent-a",
        lease.attempt,
        WorkResult(work_unit_id="u0", status=WorkResultStatus.FAILED, error="worker failed"),
    )

    status = await store.job_status(job_id)
    assert status is not None and status.state is JobState.DONE
    assert await store.completed_result_ref("u0") is None


async def test_completion_rejects_stale_attempt_from_same_agent(
    store: SqliteQueueStore,
) -> None:
    job_id = await submit(store, make_units(1))
    first = await store.lease_next("agent-a", ["m1"])
    assert first is not None
    result = succeeded("u0")
    assert result.result_ref is not None
    assert await store.bind_result_payload(
        "agent-a", "u0", first.attempt, "c" * 64, result.result_ref
    )
    assert await store.requeue_agent("agent-a") == 1
    second = await store.lease_next("agent-a", ["m1"])
    assert second is not None and second.attempt == first.attempt + 1

    await store.complete_unit("agent-a", first.attempt, result)

    status = await store.job_status(job_id)
    assert status is not None and status.state is JobState.RUNNING


async def test_resubmit_clears_incomplete_attempt_binding(store: SqliteQueueStore) -> None:
    units = make_units(1)
    await submit(store, units)
    first = await store.lease_next("agent-a", ["m1"])
    assert first is not None
    assert await store.bind_result_payload(
        "agent-a", "u0", first.attempt, "d" * 64, "result://first"
    )

    await submit(store, units)
    second = await store.lease_next("agent-b", ["m1"])
    assert second is not None and second.attempt == 1

    assert await store.bind_result_payload(
        "agent-b", "u0", second.attempt, "e" * 64, "result://second"
    )
