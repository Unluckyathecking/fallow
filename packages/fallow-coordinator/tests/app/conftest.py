"""Fixtures: a coordinator app over tmp dirs + a frozen clock, driven via ASGI.

The lifespan is entered manually (``app.router.lifespan_context``) so the stores
open and the background loops start; ``requeue_interval_s`` is set large so those
loops tick once and then sleep past the end of every test (cancelled on exit).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest_asyncio
from app_helpers import ADMIN_KEY, FakeClock
from httpx import ASGITransport

from fallow_coordinator.app import CoordinatorConfig, create_app


@dataclass
class Harness:
    client: httpx.AsyncClient
    clock: FakeClock
    config: CoordinatorConfig


def _make_config(tmp_path: Path, chunks_per_unit: int) -> CoordinatorConfig:
    return CoordinatorConfig(
        db_path=tmp_path / "coordinator.db",
        blob_dir=tmp_path / "blobs",
        unit_input_dir=tmp_path / "units",
        events_jsonl_path=tmp_path / "events.jsonl",
        gateway_log_path=tmp_path / "gateway.jsonl",
        admin_key=ADMIN_KEY,
        requeue_interval_s=3600.0,
        poll_sleep_s=0.01,
        chunks_per_unit=chunks_per_unit,
    )


@pytest_asyncio.fixture
async def harness(tmp_path: Path) -> AsyncIterator[Harness]:
    async for h in _harness_with(tmp_path, chunks_per_unit=32):
        yield h


@pytest_asyncio.fixture
async def harness_small_chunks(tmp_path: Path) -> AsyncIterator[Harness]:
    async for h in _harness_with(tmp_path, chunks_per_unit=2):
        yield h


async def _harness_with(tmp_path: Path, chunks_per_unit: int) -> AsyncIterator[Harness]:
    clock = FakeClock()
    config = _make_config(tmp_path, chunks_per_unit)
    app = create_app(config, now=clock, sleep=asyncio.sleep)
    async with app.router.lifespan_context(app):
        client = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://coord")
        try:
            yield Harness(client=client, clock=clock, config=config)
        finally:
            await client.aclose()
