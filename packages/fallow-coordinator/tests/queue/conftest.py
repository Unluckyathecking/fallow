"""Shared fixtures for the C1 queue-store tests.

Everything is deterministic: a FakeClock drives all timestamps, and the store
runs against a throwaway SQLite file under ``tmp_path``. No network, no real
llama-server, no GPU.
"""

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio

from fallow_coordinator.queue import SqliteQueueStore
from fallow_protocol.capabilities import WorkerKind
from fallow_protocol.messages import (
    JobSubmit,
    WorkMetrics,
    WorkResult,
    WorkResultStatus,
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


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest_asyncio.fixture
async def store(tmp_path: Path, clock: FakeClock) -> AsyncIterator[SqliteQueueStore]:
    s = SqliteQueueStore(
        tmp_path / "queue.db", now=clock, max_attempts=4, default_lease_s=LEASE_FLOOR_S
    )
    await s.init()
    try:
        yield s
    finally:
        await s.close()


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


async def submit(
    store: SqliteQueueStore,
    units: Sequence[WorkUnitSpec],
    model_id: str = "m1",
    priority: int = 0,
) -> str:
    return await store.submit_job(make_job(model_id, priority), list(units))
