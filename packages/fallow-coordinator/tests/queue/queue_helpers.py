"""Shared clock and builders for the C1 queue-store tests.

Everything is deterministic: a FakeClock drives all timestamps, and the store
runs against a throwaway SQLite file under ``tmp_path``. No network, no real
llama-server, no GPU.

Import helpers from this module (never from ``conftest``): pytest imports each
``conftest.py`` under a private internal name, so ``from conftest import X`` is
resolved by sys.path insertion order and breaks when test trees are mixed.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from fallow_coordinator.queue import SqliteQueueStore
from fallow_protocol.capabilities import WorkerKind
from fallow_protocol.messages import (
    JobSubmit,
    WorkMetrics,
    WorkResult,
    WorkResultStatus,
    WorkUnitLease,
    WorkUnitSpec,
)

START = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

# Fixture default lease floor; units carry no estimate so the lease window is a
# clean, predictable LEASE_FLOOR_S seconds (see lease_window_s formula).
LEASE_FLOOR_S = 20.0


class FakeClock:
    """Monotonic, manually advanced UTC clock."""

    def __init__(self, start: datetime = START) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


def make_job(model_id: str = "m1", priority: int = 0) -> JobSubmit:
    return JobSubmit(
        kind=WorkerKind.EMBED,
        model_id=model_id,
        payload_ref="corpus://ref",
        priority=priority,
    )


def make_units(count: int, prefix: str = "u", est: float | None = None) -> list[WorkUnitSpec]:
    return [
        WorkUnitSpec(
            work_unit_id=f"{prefix}{i}",
            idx=i,
            input_ref=f"input://{prefix}{i}",
            est_duration_s=est,
        )
        for i in range(count)
    ]


def succeeded(work_unit_id: str) -> WorkResult:
    return WorkResult(
        work_unit_id=work_unit_id,
        status=WorkResultStatus.SUCCEEDED,
        result_ref=f"result://{work_unit_id}",
        metrics=WorkMetrics(duration_s=1.0, items=3),
    )


async def complete_succeeded(store: SqliteQueueStore, agent_id: str, lease: WorkUnitLease) -> None:
    """Bind and complete one successful result for its current lease attempt."""
    result = succeeded(lease.work_unit_id)
    assert result.result_ref is not None
    assert await store.bind_result_payload(
        agent_id,
        lease.work_unit_id,
        lease.attempt,
        digest=lease.work_unit_id.encode().hex().ljust(64, "0")[:64],
        result_ref=result.result_ref,
    )
    await store.complete_unit(agent_id, lease.attempt, result)


async def submit(
    store: SqliteQueueStore,
    units: Sequence[WorkUnitSpec],
    model_id: str = "m1",
    priority: int = 0,
) -> str:
    return await store.submit_job(make_job(model_id, priority), list(units))
