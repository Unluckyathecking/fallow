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
from fallow_coordinator.app.standby import run_export_loop
from fallow_coordinator.app.state import (
    Clock,
    CoordinatorState,
    Monotonic,
    RelayFailureFlags,
    Sleeper,
)
from fallow_coordinator.discovery import (
    Advertisement,
    SiteAdvertiser,
    ZeroconfAdvertiser,
    build_advertisement,
)
from fallow_coordinator.gateway import (
    GatewayConfig,
    InflightTracker,
    JsonlRequestLog,
    QuotaManager,
    create_gateway_router,
)
from fallow_coordinator.gateway.errors import (
    TYPE_INVALID_REQUEST,
    TYPE_NO_REPLICA,
    TYPE_UPSTREAM,
)
from fallow_coordinator.gateway.protocols import SiteRoute, SiteRouteResolver
from fallow_coordinator.gateway.proxy import (
    PassThrough,
    ProxyRequest,
    ReplicaTransport,
    SiteAwareTransport,
    SiteRelayTransport,
    UpstreamProxy,
)
from fallow_coordinator.gateway.ragcontext import ChunkRetriever, RagRetrievalError
from fallow_coordinator.modelserve import (
    MeshManifestBuilder,
    create_mesh_router,
    create_modelserve_router,
)
from fallow_coordinator.queue import SqliteQueueStore
from fallow_coordinator.rag import (
    RagVectorStore,
    ReplicaPicker,
    RetrievalError,
    VectorSink,
    create_query_router,
    find_collection,
    search_collection,
)
from fallow_coordinator.rag.retrieval import EmbedFetch, EmbedReply, http_embed_fetch
from fallow_coordinator.registry import ApiKeyInfo, RegistryConfig, SqliteRegistry, Transport
from fallow_coordinator.scheduler import (
    CapabilityScheduler,
    ChurnAwareScheduler,
    ChurnModel,
    DispatchLoop,
    RoundRobinScheduler,
    build_churn_model,
    build_reliability_model,
)
from fallow_coordinator.site import build_site_admin_router
from fallow_coordinator.site_relay import RelayBroker
from fallow_protocol.interfaces import SchedulerPolicy
from fallow_protocol.messages import ReplicaEndpoint

# relay-v1 grants each claim a 30-second deadline (deadline_ms 30000).
_SITE_RELAY_DEADLINE_S = 30.0

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
    advertiser: SiteAdvertiser | None = None,
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
        client=http_client or _build_http_client(config),
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
        churn=_build_backup_churn(config),
    )
    mdns = _build_mdns(config, advertiser)
    app = FastAPI(title="fallow-coordinator", lifespan=_make_lifespan(state, mdns))
    app.state.coordinator = state
    # Site Mode: build the relay and the persisted-transport resolver, then mount
    # the admin join-bundle router. Left off entirely when site mode is disabled,
    # so the direct HTTP path and its routes are unchanged.
    if config.site.enabled:
        state.relay = RelayBroker()
        state.site_route = _build_site_route_resolver(state)
        state.relay_flags = RelayFailureFlags()
        app.include_router(
            build_site_admin_router(config, lambda: registry.create_enrollment_token(mode="site"))
        )
    embed_fetch = _build_embed_fetch(state)
    # One inflight-enriched, policy-delegating picker, shared so RAG embeds route
    # through the same pick path as chat traffic (not a blind endpoints[0]).
    inflight_holder: dict[str, GetInflight] = {}
    pick = _build_enriched_pick(state, inflight_holder)
    gateway_router = _build_gateway_router(state, pick)
    inflight_holder["get"] = getattr(gateway_router, "get_inflight")  # noqa: B009 - router seam
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
    if config.modelmesh_signing_key is not None:
        builder = MeshManifestBuilder(config.modelmesh_signing_key.encode("utf-8"))
        app.include_router(create_mesh_router(registry, builder))
    app.include_router(create_query_router(registry, rag, state.client, clock, pick, embed_fetch))
    return app


def build_app() -> FastAPI:
    """No-arg factory for ``uvicorn fallow_coordinator.app:build_app --factory``.

    Uvicorn's ``--factory`` path only receives the app object, so it cannot apply
    the Site Mode TLS certificate/key or the validated exact bind — the CLI
    ``--host`` and a missing certificate would serve the coordinator over
    cleartext on the LAN. Fail closed under Site Mode and point operators at
    ``python -m fallow_coordinator serve``, which passes both. Site Mode disabled
    keeps today's HTTP factory behaviour.
    """
    raw = os.environ.get(CONFIG_ENV)
    path = Path(raw).expanduser() if raw else DEFAULT_CONFIG_PATH.expanduser()
    config = load_config(path)
    if config.site.enabled:
        raise RuntimeError(
            "Site Mode cannot start through the uvicorn --factory path: it applies "
            "neither the TLS certificate/key nor the validated exact bind. Run "
            "'python -m fallow_coordinator serve --config <path>' instead."
        )
    return create_app(config)


def _default_clock() -> datetime:
    return datetime.now(UTC)


def _build_policy(config: CoordinatorConfig, clock: Clock) -> SchedulerPolicy:
    """Select the experiment-arm scheduler named in the config.

    ``churn_v2`` builds its empirical idle-survival model once at startup from the
    configured churn history file, and its per-agent task-success reliability model
    from the ``units.jsonl`` sibling of that same history — the prior run's unit
    lifecycle log. Both train from the historical run, never from the current run's
    output sinks, so a mid-run restart cannot contaminate the snapshot. A missing
    or empty history yields an empty model that falls back to the optimistic prior
    everywhere. The current hour-of-day comes from the injected clock so the arm
    stays deterministic.
    """
    if config.scheduler == "roundrobin":
        return RoundRobinScheduler()
    if config.scheduler == "churn_v2":
        model = build_churn_model(_load_events(config.churn_history_jsonl_path), _utc_hour)
        units_history = config.churn_history_jsonl_path.with_name("units.jsonl")
        reliability = build_reliability_model(_load_events(units_history))
        return ChurnAwareScheduler(
            model,
            config.churn_est_unit_duration_s,
            hour_fn=lambda: clock().hour,
            reliability=reliability,
        )
    return CapabilityScheduler()


def _build_backup_churn(config: CoordinatorConfig) -> ChurnModel | None:
    """Idle-survival model for the speculative backup decision (ADR 056), or None.

    Built only when the feature is enabled, from the same startup churn history
    the churn_v2 arm trains on — independent of the scheduler arm, so backups work
    under any policy. A missing or empty history yields an empty model whose
    survival is the optimistic prior everywhere, so speculation is conservative
    until sessions accumulate.
    """
    if not config.speculative_backup_enabled:
        return None
    return build_churn_model(_load_events(config.churn_history_jsonl_path), _utc_hour)


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


def _build_http_client(config: CoordinatorConfig) -> httpx.AsyncClient:
    """The shared upstream client. Under Site Mode it ignores proxy env vars.

    A school network's ``HTTP_PROXY``/``ALL_PROXY`` must not silently reroute the
    coordinator's replica dials, so Site Mode uses ``trust_env=False``. The default
    HTTP path keeps httpx's env-aware behaviour. Injected test clients are never
    replaced.
    """
    return httpx.AsyncClient(
        timeout=GatewayConfig().httpx_timeout(),
        trust_env=not config.site.enabled,
    )


def _build_enriched_pick(state: CoordinatorState, holder: dict[str, GetInflight]) -> ReplicaPicker:
    """The scheduler pick, enriched with live inflight counts from ``holder``."""

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

    return enriched_pick


def _build_gateway_router(state: CoordinatorState, pick: ReplicaPicker) -> APIRouter:
    """Mount the gateway over the shared inflight-enriched, policy-delegating pick."""
    gateway_config = GatewayConfig(
        admission_timeout_s=state.config.admission_timeout_s,
        admission_capacity=state.config.admission_capacity,
        affinity_ttl_s=state.config.affinity_ttl_s,
        affinity_max=state.config.affinity_max,
    )
    build_transport = (
        _build_site_transport_builder(state, gateway_config)
        if state.relay is not None and state.site_route is not None
        else None
    )
    return create_gateway_router(
        state.registry,
        pick,
        state.client,
        gateway_config,
        JsonlRequestLog(state.config.gateway_log_path),
        state.now,
        state.monotonic,
        state.sleep,
        state.quotas,
        _build_retriever(state, pick),
        build_transport,
    )


def _build_embed_fetch(state: CoordinatorState) -> EmbedFetch:
    """The embed transport for RAG: relay for a site agent, direct HTTP otherwise.

    Mirrors the chat path so a ``site_relay`` embedding replica is reached through
    the broker and never dialed on its registered host. With Site Mode off this is
    the plain HTTP fetch, so existing RAG behaviour is unchanged.
    """
    direct = http_embed_fetch(state.client)
    if state.relay is None or state.site_route is None:
        return direct
    resolve = state.site_route
    relay = SiteRelayTransport(
        state.relay,
        deadline_s=_SITE_RELAY_DEADLINE_S,
        monotonic=state.monotonic,
        first_byte_timeout_s=GatewayConfig().first_byte_timeout_s,
    )

    async def fetch(endpoint: ReplicaEndpoint, body: dict[str, object]) -> EmbedReply:
        if await resolve(endpoint.agent_id) is None:
            return await direct(endpoint, body)
        request = ProxyRequest(
            method="POST",
            path="/v1/embeddings",
            body=json.dumps(body).encode("utf-8"),
            content_type="application/json",
        )
        outcome = await relay.try_buffered(request, endpoint)
        if isinstance(outcome, PassThrough):
            return EmbedReply(outcome.status_code, outcome.body)
        return EmbedReply(502, b"")  # relay failure: embed_query retries the next pick

    return fetch


def _build_mdns(
    config: CoordinatorConfig, advertiser: SiteAdvertiser | None
) -> tuple[SiteAdvertiser, Advertisement] | None:
    """The Site Mode mDNS advertiser and its record, or ``None`` when mDNS is off.

    The interface is resolved here rather than in the lifespan so an invalid or
    ambiguous ``host`` fails ``create_app`` before the listener binds. With
    ``[site].mdns_service`` unset nothing is built and nothing is registered, so
    an injected test double stays untouched and the default path opens no socket.
    """
    site = config.site
    if not site.enabled or site.mdns_service is None:
        return None
    assert site.site_id is not None  # guaranteed by the site config validator
    record = build_advertisement(site_id=site.site_id, host=config.host, port=config.port)
    return advertiser if advertiser is not None else ZeroconfAdvertiser(), record


def _build_site_route_resolver(state: CoordinatorState) -> SiteRouteResolver:
    """Resolve an agent's persisted Site Mode route, or ``None`` for a direct agent.

    ADR 081 hands the router its transport through this injected callback. The
    persisted ``transport`` and ``presence_generation`` live on the registry's
    agent row; this app-layer closure is the wiring that reads them so the gateway
    and relay routes stay decoupled from the registry's storage.
    """

    async def resolve(agent_id: str) -> SiteRoute | None:
        route = await state.registry.site_route(agent_id)
        if route is None or route[0] != Transport.SITE_RELAY:
            return None
        return SiteRoute(presence_generation=route[1])

    return resolve


def _build_site_transport_builder(
    state: CoordinatorState, gateway_config: GatewayConfig
) -> Callable[[InflightTracker], ReplicaTransport]:
    """The transport builder that routes site agents through the relay.

    Runs against the tracker the gateway router owns, so live inflight counts stay
    single-sourced. A ``direct`` agent still routes byte-for-byte through the
    :class:`UpstreamProxy`.
    """

    def build(tracker: InflightTracker) -> ReplicaTransport:
        assert state.relay is not None and state.site_route is not None
        direct = UpstreamProxy(state.client, gateway_config, tracker)
        relay = SiteRelayTransport(
            state.relay,
            deadline_s=_SITE_RELAY_DEADLINE_S,
            monotonic=state.monotonic,
            first_byte_timeout_s=gateway_config.first_byte_timeout_s,
            take_nonretryable=(state.relay_flags.take if state.relay_flags is not None else None),
        )
        return SiteAwareTransport(direct, relay, state.site_route, tracker)

    return build


_RETRIEVAL_ERROR_TYPES = {
    404: TYPE_INVALID_REQUEST,
    502: TYPE_UPSTREAM,
    503: TYPE_NO_REPLICA,
}


def _build_retriever(state: CoordinatorState, pick: ReplicaPicker) -> ChunkRetriever:
    """Adapt the RAG retrieval core to the gateway's injected retriever seam.

    The gateway and RAG package are dependency-graph siblings, so this app-level
    closure is where their error vocabularies meet: a RAG ``RetrievalError`` is
    re-raised as the gateway's OpenAI-envelope ``RagRetrievalError``. It also
    enforces the calling key's model allowlist against the collection's embedding
    model — the same check the query route applies — before embedding anything.
    """
    fetch = _build_embed_fetch(state)

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
                state.registry, state.rag, state.client, state.now, found, query, k, pick, fetch
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
    mdns: tuple[SiteAdvertiser, Advertisement] | None,
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
            if state.config.standby_path is not None:
                state.tasks.append(
                    asyncio.create_task(
                        run_export_loop(
                            source_db=state.config.db_path,
                            dest=state.config.standby_path,
                            interval_s=state.config.standby_export_interval_s,
                            sleep=state.sleep,
                            stop_event=state.stop_event,
                        )
                    )
                )
            # Advertise only once the stores are open and the loops are running:
            # the record points at a coordinator that can already answer.
            if mdns is not None:
                await mdns[0].register(mdns[1])
            yield
        finally:
            if mdns is not None:
                await mdns[0].unregister()
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
