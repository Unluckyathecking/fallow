"""Fixtures: a coordinator app over tmp dirs + a frozen clock, driven via ASGI.

The lifespan is entered manually (``app.router.lifespan_context``) so the stores
open and the background loops start; ``requeue_interval_s`` is set large so those
loops tick once and then sleep past the end of every test (cancelled on exit).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest_asyncio
from app_helpers import ADMIN_KEY, FakeClock, Harness
from httpx import ASGITransport

from fallow_coordinator.app import CoordinatorConfig, create_app


def _make_config(
    tmp_path: Path, chunks_per_unit: int, max_result_payload_bytes: int = 64 * 1024 * 1024
) -> CoordinatorConfig:
    return CoordinatorConfig(
        db_path=tmp_path / "coordinator.db",
        blob_dir=tmp_path / "blobs",
        unit_input_dir=tmp_path / "units",
        result_dir=tmp_path / "results",
        events_jsonl_path=tmp_path / "events.jsonl",
        gateway_log_path=tmp_path / "gateway.jsonl",
        admin_key=ADMIN_KEY,
        requeue_interval_s=3600.0,
        poll_sleep_s=0.01,
        chunks_per_unit=chunks_per_unit,
        max_result_payload_bytes=max_result_payload_bytes,
        admission_timeout_s=0,
    )


@pytest_asyncio.fixture
async def harness(tmp_path: Path) -> AsyncIterator[Harness]:
    async for h in _harness_with(tmp_path, chunks_per_unit=32):
        yield h


@pytest_asyncio.fixture
async def harness_small_chunks(tmp_path: Path) -> AsyncIterator[Harness]:
    async for h in _harness_with(tmp_path, chunks_per_unit=2):
        yield h


@pytest_asyncio.fixture
async def harness_small_payload(tmp_path: Path) -> AsyncIterator[Harness]:
    async for h in _harness_with(tmp_path, chunks_per_unit=32, max_result_payload_bytes=5):
        yield h


async def _harness_with(
    tmp_path: Path,
    chunks_per_unit: int,
    max_result_payload_bytes: int = 64 * 1024 * 1024,
) -> AsyncIterator[Harness]:
    clock = FakeClock()
    config = _make_config(tmp_path, chunks_per_unit, max_result_payload_bytes)
    app = create_app(config, now=clock, sleep=asyncio.sleep)
    async with app.router.lifespan_context(app):
        client = httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://coord")
        try:
            yield Harness(client=client, clock=clock, config=config)
        finally:
            await client.aclose()
