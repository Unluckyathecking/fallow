"""Fixtures for the I3 integration suite: a real coordinator app over ASGI.

Each harness runs the real ``create_app`` inside its own lifespan (stores opened,
background loops started) with an injected :class:`FakeClock` and real
``asyncio.sleep``. The ASGI transport pins its client address to loopback so the
registration host the registry records — and therefore the gateway's replica
endpoints — is ``127.0.0.1``. A factory fixture lets each scenario tune the
config (chunk size, liveness thresholds, requeue cadence) without a bespoke
fixture per test.

The ``goagent`` fixtures below add a second flavour: the same coordinator served
over a real loopback socket so the built Go agent can drive it. They are used
only by the ``@pytest.mark.goagent`` parity scenarios and skip when no binary is
present; the in-process ASGI path above is untouched.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from goagent import GoAgent
from httpx import ASGITransport
from integration_helpers import ADMIN_KEY, FakeClock, Harness, HarnessFactory
from live_coordinator import LiveCoordinator, serve_app

from fallow_coordinator.app import CoordinatorConfig, create_app

_ASGI_CLIENT = ("127.0.0.1", 9999)
_GO_AGENT_BIN_ENV = "FALLOW_GO_AGENT_BIN"


def _make_config(tmp_path: Path, **overrides: object) -> CoordinatorConfig:
    base: dict[str, object] = {
        "db_path": tmp_path / "coordinator.db",
        "blob_dir": tmp_path / "blobs",
        "unit_input_dir": tmp_path / "units",
        "result_dir": tmp_path / "results",
        "events_jsonl_path": tmp_path / "events.jsonl",
        "gateway_log_path": tmp_path / "gateway.jsonl",
        "admin_key": ADMIN_KEY,
        "requeue_interval_s": 3600.0,
        "poll_sleep_s": 0.01,
        "admission_timeout_s": 0,
    }
    base.update(overrides)
    return CoordinatorConfig.model_validate(base)


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


@pytest.fixture(scope="session")
def go_agent_binary() -> Path:
    """Path to the built Go agent binary, or skip if it is absent.

    CI's Go job builds ``cmd/agentctl`` and exports ``FALLOW_GO_AGENT_BIN``.
    Locally the parity scenarios skip cleanly when the variable is unset or the
    path does not exist.
    """
    raw = os.environ.get(_GO_AGENT_BIN_ENV)
    if not raw:
        pytest.skip(f"{_GO_AGENT_BIN_ENV} not set; build cmd/agentctl to run goagent parity")
    binary = Path(raw)
    if not binary.is_file():
        pytest.skip(f"{_GO_AGENT_BIN_ENV}={raw} is not a file")
    return binary


@pytest_asyncio.fixture
async def make_live_coordinator(tmp_path: Path) -> AsyncIterator[object]:
    """Yield a factory serving real coordinators over loopback; all torn down at end.

    Mirrors :func:`make_harness` but over a real socket, sharing the injected
    ``FakeClock`` so scenarios can advance time to drive eviction and requeue.
    """
    stack = AsyncExitStack()
    counter = {"n": 0}

    async def factory(**overrides: object) -> LiveCoordinator:
        counter["n"] += 1
        sub = tmp_path / f"live{counter['n']}"
        sub.mkdir()
        clock = FakeClock()
        config = _make_config(sub, **overrides)
        app = create_app(config, now=clock, sleep=asyncio.sleep)
        base_url = await stack.enter_async_context(serve_app(app))
        client = await stack.enter_async_context(httpx.AsyncClient(base_url=base_url))
        return LiveCoordinator(base_url=base_url, client=client, clock=clock, config=config)

    try:
        yield factory
    finally:
        await stack.aclose()


def make_go_agent(binary: Path, coordinator: LiveCoordinator) -> GoAgent:
    """Build a Go agent uplink bound to a live coordinator's base URL."""
    return GoAgent(binary=binary, base_url=coordinator.base_url)
