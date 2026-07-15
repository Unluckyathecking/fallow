"""Fixtures for the C1 queue-store tests.

Non-fixture builders and the FakeClock live in ``queue_helpers``; conftest is
fixtures-only. Everything is deterministic: a FakeClock drives all timestamps,
and the store runs against a throwaway SQLite file under ``tmp_path``. No
network, no real llama-server, no GPU.
"""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from queue_helpers import LEASE_FLOOR_S, FakeClock

from fallow_coordinator.queue import SqliteQueueStore


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
