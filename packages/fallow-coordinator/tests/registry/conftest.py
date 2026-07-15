"""Fixtures for registry tests: a FakeClock and an opened temp-file registry."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from registry_helpers import ADMIN_KEY, FakeClock

from fallow_coordinator.registry import RegistryConfig, SqliteRegistry


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest_asyncio.fixture
async def registry(clock: FakeClock, tmp_path: Path) -> AsyncIterator[SqliteRegistry]:
    config = RegistryConfig(admin_key=ADMIN_KEY)
    store = SqliteRegistry(tmp_path / "registry.db", config, clock)
    await store.open()
    try:
        yield store
    finally:
        await store.close()
