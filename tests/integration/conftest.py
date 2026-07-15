"""Fixtures for the I3 integration suite: a real coordinator app over ASGI.

Each harness runs the real ``create_app`` inside its own lifespan (stores opened,
background loops started) with an injected :class:`FakeClock` and real
``asyncio.sleep``. The ASGI transport pins its client address to loopback so the
registration host the registry records — and therefore the gateway's replica
endpoints — is ``127.0.0.1``. A factory fixture lets each scenario tune the
config (chunk size, liveness thresholds, requeue cadence) without a bespoke
fixture per test.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest_asyncio
from httpx import ASGITransport
from integration_helpers import ADMIN_KEY, FakeClock

from fallow_coordinator.app import CoordinatorConfig, create_app

_ASGI_CLIENT = ("127.0.0.1", 9999)


@dataclass
class Harness:
    """One live coordinator app plus the raw ASGI client and its clock/config."""

    client: httpx.AsyncClient
    clock: FakeClock
    config: CoordinatorConfig


def _make_config(tmp_path: Path, **overrides: object) -> CoordinatorConfig:
    base: dict[str, object] = {
        "db_path": tmp_path / "coordinator.db",
        "blob_dir": tmp_path / "blobs",
        "unit_input_dir": tmp_path / "units",
        "events_jsonl_path": tmp_path / "events.jsonl",
        "gateway_log_path": tmp_path / "gateway.jsonl",
        "admin_key": ADMIN_KEY,
        "requeue_interval_s": 3600.0,
        "poll_sleep_s": 0.01,
    }
    base.update(overrides)
    return CoordinatorConfig.model_validate(base)


HarnessFactory = Callable[..., Awaitable[Harness]]


@pytest_asyncio.fixture
async def make_harness(tmp_path: Path) -> AsyncIterator[HarnessFactory]:
    """Yield a factory building live coordinator harnesses; all torn down at end."""
    stack = AsyncExitStack()
    counter = {"n": 0}

    async def factory(**overrides: object) -> Harness:
        counter["n"] += 1
        sub = tmp_path / f"coord{counter['n']}"
        sub.mkdir()
        clock = FakeClock()
        config = _make_config(sub, **overrides)
        app = create_app(config, now=clock, sleep=asyncio.sleep)
        await stack.enter_async_context(app.router.lifespan_context(app))
        transport = ASGITransport(app=app, client=_ASGI_CLIENT)
        client = await stack.enter_async_context(
            httpx.AsyncClient(transport=transport, base_url="http://coord")
        )
        return Harness(client=client, clock=clock, config=config)

    try:
        yield factory
    finally:
        await stack.aclose()
