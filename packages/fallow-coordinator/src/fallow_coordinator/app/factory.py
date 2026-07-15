"""Coordinator FastAPI app factory (module I1).

``create_app`` builds every collaborator synchronously (so the routers can be
mounted before the server starts), then a lifespan opens the two SQLite stores on
the one shared database file and starts the background maintenance loops. The
clock and sleeper are injectable so the whole app — long-poll deadlines,
liveness maths, background cadence — is deterministic under test.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
from fastapi import APIRouter, FastAPI

from fallow_coordinator.app.admin_routes import build_admin_router
from fallow_coordinator.app.agent_routes import build_agent_router
from fallow_coordinator.app.background import offline_eviction_loop, snapshot_source
from fallow_coordinator.app.config import CoordinatorConfig, load_config
from fallow_coordinator.app.events import EventStateOverrides, EventsWriter
from fallow_coordinator.app.state import Clock, CoordinatorState, Sleeper
from fallow_coordinator.gateway import GatewayConfig, JsonlRequestLog, create_gateway_router
from fallow_coordinator.modelserve import create_modelserve_router
from fallow_coordinator.queue import SqliteQueueStore
from fallow_coordinator.registry import RegistryConfig, SqliteRegistry
from fallow_coordinator.scheduler import CapabilityScheduler, DispatchLoop
from fallow_protocol.messages import ReplicaEndpoint

# Where ``build_app()`` (uvicorn ``--factory``) looks for its config.
CONFIG_ENV = "FLW_COORDINATOR_CONFIG"
DEFAULT_CONFIG_PATH = Path("~/.fallow/coordinator.toml")

GetInflight = Callable[[], dict[tuple[str, int], int]]


def create_app(
    config: CoordinatorConfig,
    *,
    now: Clock | None = None,
    sleep: Sleeper | None = None,
    token_factory: Callable[[], str] | None = None,
) -> FastAPI:
    """Build the coordinator app (stores are opened later, in the lifespan)."""
    clock: Clock = now if now is not None else _default_clock
    sleeper: Sleeper = sleep if sleep is not None else asyncio.sleep
    _ensure_dirs(config)
    registry = _build_registry(config, clock, token_factory)
    state = CoordinatorState(
        config=config,
        registry=registry,
        queue=SqliteQueueStore(config.db_path, now=clock),
        policy=CapabilityScheduler(),
        now=clock,
        sleep=sleeper,
        client=httpx.AsyncClient(timeout=GatewayConfig().httpx_timeout()),
        events=EventsWriter(config.events_jsonl_path),
        overrides=EventStateOverrides(),
    )
    app = FastAPI(title="fallow-coordinator", lifespan=_make_lifespan(state))
    app.state.coordinator = state
    app.include_router(build_agent_router(state))
    app.include_router(build_admin_router(state))
    app.include_router(_build_gateway_router(state))
    app.include_router(create_modelserve_router(registry))
    return app


def build_app() -> FastAPI:
    """No-arg factory for ``uvicorn fallow_coordinator.app:build_app --factory``."""
    raw = os.environ.get(CONFIG_ENV)
    path = Path(raw).expanduser() if raw else DEFAULT_CONFIG_PATH.expanduser()
    return create_app(load_config(path))


def _default_clock() -> datetime:
    return datetime.now(UTC)


def _build_registry(
    config: CoordinatorConfig, clock: Clock, token_factory: Callable[[], str] | None
) -> SqliteRegistry:
    registry_config = RegistryConfig(
        admin_key=config.admin_key,
        suspect_after_s=config.suspect_after_s,
        offline_after_s=config.offline_after_s,
    )
    if token_factory is None:
        return SqliteRegistry(config.db_path, registry_config, clock)
    return SqliteRegistry(config.db_path, registry_config, clock, token_factory)


def _ensure_dirs(config: CoordinatorConfig) -> None:
    config.blob_dir.mkdir(parents=True, exist_ok=True)
    config.unit_input_dir.mkdir(parents=True, exist_ok=True)
    for file_path in (config.db_path, config.events_jsonl_path, config.gateway_log_path):
        file_path.parent.mkdir(parents=True, exist_ok=True)


def _build_gateway_router(state: CoordinatorState) -> APIRouter:
    """Mount the gateway with an inflight-enriched, policy-delegating replica pick."""
    holder: dict[str, GetInflight] = {}

    def enriched_pick(model_id: str, replicas: Sequence[ReplicaEndpoint]) -> ReplicaEndpoint | None:
        getter = holder.get("get")
        counts = getter() if getter is not None else {}
        merged = tuple(
            replica.model_copy(
                update={"inflight": counts.get((replica.host, replica.port), replica.inflight)}
            )
            for replica in replicas
        )
        return state.policy.pick_replica(model_id, merged)

    router = create_gateway_router(
        state.registry,
        enriched_pick,
        state.client,
        GatewayConfig(),
        JsonlRequestLog(state.config.gateway_log_path),
        state.now,
    )
    holder["get"] = getattr(router, "get_inflight")  # noqa: B009 - dynamic router seam
    return router


def _make_lifespan(
    state: CoordinatorState,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await state.registry.open()
        await state.queue.init()
        dispatch = DispatchLoop(
            state.queue,
            lambda: snapshot_source(state),
            state.policy,
            state.config.requeue_interval_s,
            state.now,
            state.sleep,
        )
        state.dispatch = dispatch
        state.tasks = [
            asyncio.create_task(dispatch.run_forever()),
            asyncio.create_task(offline_eviction_loop(state)),
        ]
        try:
            yield
        finally:
            await _shutdown(state)

    return lifespan


async def _shutdown(state: CoordinatorState) -> None:
    state.stop_event.set()
    if state.dispatch is not None:
        state.dispatch.stop()
    for task in state.tasks:
        task.cancel()
    for task in state.tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task
    await state.client.aclose()
    await state.queue.close()
    await state.registry.close()
