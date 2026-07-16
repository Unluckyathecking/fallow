"""Coordinator FastAPI app factory (module I1).

``create_app`` builds every collaborator synchronously so the routers can be
mounted before the server starts. Its lifespan opens the registry and queue on
the coordinator database, opens the sibling RAG database, and starts the
background maintenance loops. The injected clock and sleeper keep time-based
behavior deterministic under test.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
from fastapi import APIRouter, FastAPI

from fallow_coordinator.app.admin_routes import build_admin_router, build_metrics_router
from fallow_coordinator.app.agent_routes import build_agent_router
from fallow_coordinator.app.background import (
    offline_eviction_loop,
    quota_snapshot_loop,
    snapshot_source,
)
from fallow_coordinator.app.config import CoordinatorConfig, load_config
from fallow_coordinator.app.events import EventStateOverrides, EventsWriter, UnitsWriter
from fallow_coordinator.app.metrics import GetInflight
from fallow_coordinator.app.rag_ingestion import IngestionService
from fallow_coordinator.app.result_blobs import ResultBlobStore
from fallow_coordinator.app.state import Clock, CoordinatorState, Monotonic, Sleeper
from fallow_coordinator.gateway import (
    GatewayConfig,
    JsonlRequestLog,
    QuotaManager,
    create_gateway_router,
)
from fallow_coordinator.gateway.errors import (
    TYPE_INVALID_REQUEST,
    TYPE_NO_REPLICA,
    TYPE_UPSTREAM,
)
from fallow_coordinator.gateway.ragcontext import ChunkRetriever, RagRetrievalError
from fallow_coordinator.modelserve import create_modelserve_router
from fallow_coordinator.queue import SqliteQueueStore
from fallow_coordinator.rag import (
    RagVectorStore,
    RetrievalError,
    VectorSink,
    create_query_router,
    find_collection,
    search_collection,
)
from fallow_coordinator.registry import ApiKeyInfo, RegistryConfig, SqliteRegistry
from fallow_coordinator.scheduler import (
    CapabilityScheduler,
    ChurnAwareScheduler,
    DispatchLoop,
    RoundRobinScheduler,
    build_churn_model,
)
from fallow_protocol.interfaces import SchedulerPolicy
from fallow_protocol.messages import ReplicaEndpoint

# Where ``build_app()`` (uvicorn ``--factory``) looks for its config.
CONFIG_ENV = "FLW_COORDINATOR_CONFIG"
DEFAULT_CONFIG_PATH = Path("~/.fallow/coordinator.toml")


def create_app(
    config: CoordinatorConfig,
    *,
    now: Clock | None = None,
    sleep: Sleeper | None = None,
    monotonic: Monotonic | None = None,
    token_factory: Callable[[], str] | None = None,
    vector_sink: VectorSink | None = None,
    rag_store: RagVectorStore | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    """Build the coordinator app (stores are opened later, in the lifespan)."""
    clock: Clock = now if now is not None else _default_clock
    sleeper: Sleeper = sleep if sleep is not None else asyncio.sleep
    # perf_counter (not monotonic): the gateway's admission waited_ms is a
    # millisecond metric, and time.monotonic() has ~15.6 ms resolution on
    # Windows before Python 3.13. perf_counter() is high-resolution on every
    # supported platform and version.
    monotonic_clock: Monotonic = monotonic if monotonic is not None else time.perf_counter
    _ensure_dirs(config)
    registry = _build_registry(config, clock, token_factory)
    units = UnitsWriter(config.events_jsonl_path.with_name("units.jsonl"))
    queue = SqliteQueueStore(config.db_path, now=clock, on_transition=units.write)
    rag = rag_store or RagVectorStore(config.db_path.with_name("rag.db"))
    quotas = QuotaManager(registry, clock)
    state = CoordinatorState(
        config=config,
        registry=registry,
        queue=queue,
        policy=_build_policy(config, clock),
        now=clock,
        monotonic=monotonic_clock,
        sleep=sleeper,
        client=http_client or httpx.AsyncClient(timeout=GatewayConfig().httpx_timeout()),
        events=EventsWriter(config.events_jsonl_path),
        results=ResultBlobStore(config.result_dir, config.max_result_payload_bytes),
        overrides=EventStateOverrides(),
        quotas=quotas,
        rag=rag,
        ingestion=(
            IngestionService(
                queue=queue,
                sink=vector_sink or rag,
                corpus_dir=config.unit_input_dir / "rag-corpora",
                unit_input_dir=config.unit_input_dir,
                result_dir=config.result_dir,
                chunks_per_unit=config.chunks_per_unit,
            )
        ),
    )
    app = FastAPI(title="fallow-coordinator", lifespan=_make_lifespan(state))
    app.state.coordinator = state
    gateway_router = _build_gateway_router(state)
    app.include_router(build_agent_router(state))
    app.include_router(build_admin_router(state))
    app.include_router(
        build_metrics_router(
            state,
            getattr(gateway_router, "get_inflight"),  # noqa: B009 - dynamic router seam
        )
    )
    app.include_router(gateway_router)
    app.include_router(create_modelserve_router(registry))
    app.include_router(create_query_router(registry, rag, state.client, clock))
    return app


def build_app() -> FastAPI:
    """No-arg factory for ``uvicorn fallow_coordinator.app:build_app --factory``."""
    raw = os.environ.get(CONFIG_ENV)
    path = Path(raw).expanduser() if raw else DEFAULT_CONFIG_PATH.expanduser()
    return create_app(load_config(path))


def _default_clock() -> datetime:
    return datetime.now(UTC)


def _build_policy(config: CoordinatorConfig, clock: Clock) -> SchedulerPolicy:
    """Select the experiment-arm scheduler named in the config.

    ``churn_v2`` builds its empirical idle-survival model once at startup from
    the configured churn history file. A missing or empty history yields an empty
    model that falls back to the optimistic prior everywhere. The run event log
    remains an output sink and cannot alter the startup snapshot. The current
    hour-of-day comes from the injected clock so the arm stays deterministic.
    """
    if config.scheduler == "roundrobin":
        return RoundRobinScheduler()
    if config.scheduler == "churn_v2":
        model = build_churn_model(_load_events(config.churn_history_jsonl_path), _utc_hour)
        return ChurnAwareScheduler(
            model, config.churn_est_unit_duration_s, hour_fn=lambda: clock().hour
        )
    return CapabilityScheduler()


def _utc_hour(moment: datetime) -> int:
    """Hour-of-day (0-23) used to bucket a session by its start time."""
    return moment.hour


def _load_events(path: Path) -> Iterable[Mapping[str, object]]:
    """Read a JSONL history once, skipping blank or malformed lines."""
    if not path.exists():
        return []
    events: list[Mapping[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        with contextlib.suppress(json.JSONDecodeError):
            decoded = json.loads(stripped)
            if isinstance(decoded, Mapping):
                events.append(decoded)
    return events


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
    config.result_dir.mkdir(parents=True, exist_ok=True)
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
                update={
                    "inflight": max(
                        replica.inflight,
                        counts.get((replica.host, replica.port), 0),
                    )
                }
            )
            for replica in replicas
        )
        return state.policy.pick_replica(model_id, merged)

    gateway_config = GatewayConfig(
        admission_timeout_s=state.config.admission_timeout_s,
        admission_capacity=state.config.admission_capacity,
        affinity_ttl_s=state.config.affinity_ttl_s,
        affinity_max=state.config.affinity_max,
    )
    router = create_gateway_router(
        state.registry,
        enriched_pick,
        state.client,
        gateway_config,
        JsonlRequestLog(state.config.gateway_log_path),
        state.now,
        state.monotonic,
        state.sleep,
        state.quotas,
        _build_retriever(state),
    )
    holder["get"] = getattr(router, "get_inflight")  # noqa: B009 - dynamic router seam
    return router


_RETRIEVAL_ERROR_TYPES = {
    404: TYPE_INVALID_REQUEST,
    502: TYPE_UPSTREAM,
    503: TYPE_NO_REPLICA,
}


def _build_retriever(state: CoordinatorState) -> ChunkRetriever:
    """Adapt the RAG retrieval core to the gateway's injected retriever seam.

    The gateway and RAG package are dependency-graph siblings, so this app-level
    closure is where their error vocabularies meet: a RAG ``RetrievalError`` is
    re-raised as the gateway's OpenAI-envelope ``RagRetrievalError``. It also
    enforces the calling key's model allowlist against the collection's embedding
    model — the same check the query route applies — before embedding anything.
    """

    async def _retrieve(key: ApiKeyInfo, collection: str, query: str, k: int) -> tuple[str, ...]:
        try:
            found = await find_collection(state.rag, collection)
            if not _allows(key, found.model_id):
                raise RagRetrievalError(
                    403,
                    TYPE_INVALID_REQUEST,
                    f"api key not permitted to use model '{found.model_id}'",
                )
            matches = await search_collection(
                state.registry, state.rag, state.client, state.now, found, query, k
            )
        except RetrievalError as exc:
            error_type = _RETRIEVAL_ERROR_TYPES.get(exc.status_code, TYPE_INVALID_REQUEST)
            raise RagRetrievalError(exc.status_code, error_type, exc.detail) from exc
        return tuple(match.text for match in matches)

    return _retrieve


def _allows(key: ApiKeyInfo, model_id: str) -> bool:
    return key.model_allowlist is None or model_id in key.model_allowlist


def _make_lifespan(
    state: CoordinatorState,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            await state.registry.open()
            await state.queue.init()
            await state.rag.open()
            await state.quotas.restore()
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
                asyncio.create_task(quota_snapshot_loop(state)),
            ]
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
    with contextlib.suppress(Exception):
        await state.quotas.snapshot()
    await state.client.aclose()
    await state.rag.close()
    await state.queue.close()
    await state.registry.close()
