"""The OpenAI-compatible gateway router factory (module C5).

Wires three routes onto a :class:`GatewayService`. Auth is handled inside each
handler (not via a raising dependency) so failures return the OpenAI ``{"error":
{...}}`` envelope rather than FastAPI's default ``{"detail": ...}``.

The returned router carries a ``get_inflight`` attribute — the seam the app layer
reads to enrich ``ReplicaEndpoint.inflight`` with live counts before its own
call sites use the scheduler policy.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import datetime

import httpx
from fastapi import APIRouter, Request, Response

from fallow_coordinator.gateway.admission import AdmissionQueue
from fallow_coordinator.gateway.affinity import AffinityMap
from fallow_coordinator.gateway.config import GatewayConfig
from fallow_coordinator.gateway.errors import TYPE_INVALID_REQUEST, TYPE_RATE_LIMIT, openai_error
from fallow_coordinator.gateway.inflight import InflightTracker
from fallow_coordinator.gateway.protocols import GatewayRegistry, PickReplica, RequestLog
from fallow_coordinator.gateway.proxy import UpstreamProxy
from fallow_coordinator.gateway.quota import QuotaExceeded, QuotaManager
from fallow_coordinator.gateway.service import GatewayService

_CHAT_PATH = "/v1/chat/completions"
_EMBEDDINGS_PATH = "/v1/embeddings"


def create_gateway_router(
    registry: GatewayRegistry,
    pick_replica: PickReplica,
    client: httpx.AsyncClient,
    config: GatewayConfig,
    request_log: RequestLog,
    now: Callable[[], datetime],
    monotonic: Callable[[], float] = time.perf_counter,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    quotas: QuotaManager | None = None,
) -> APIRouter:
    """Build the gateway router bound to its injected collaborators.

    ``now`` is injected (not read from the wall clock) so timestamps in the
    request log and the ``replica_endpoints`` liveness query are deterministic
    under test; the app layer passes ``datetime.now(UTC)`` in production.
    """
    tracker = InflightTracker()
    admission = AdmissionQueue(
        capacity=config.admission_capacity,
        timeout_s=config.admission_timeout_s,
        poll_interval_s=config.admission_poll_interval_s,
        clock=monotonic,
        sleep=sleep,
    )
    affinity = AffinityMap(config.affinity_ttl_s, config.affinity_max, now)
    proxy = UpstreamProxy(client, config, tracker)
    service = GatewayService(
        registry=registry,
        pick_replica=pick_replica,
        proxy=proxy,
        request_log=request_log,
        now=now,
        tracker=tracker,
        inter_chunk_timeout_s=config.inter_chunk_timeout_s,
        admission=admission,
        affinity=affinity,
        quotas=quotas,
    )
    router = APIRouter()

    @router.post(_CHAT_PATH)
    async def chat_completions(request: Request) -> Response:
        return await _proxy(service, _CHAT_PATH, request)

    @router.post(_EMBEDDINGS_PATH)
    async def embeddings(request: Request) -> Response:
        return await _proxy(service, _EMBEDDINGS_PATH, request)

    @router.get("/v1/models")
    async def list_models(request: Request) -> Response:
        key = await service.authenticate(request.headers.get("authorization"))
        if key is None:
            return _unauthorized()
        exceeded = service.consume_quota(key)
        if exceeded is not None:
            return _rate_limited(exceeded)
        return await service.list_models(key)

    # Seam for the app layer: live inflight counts per (host, port).
    router.get_inflight = tracker.get_inflight  # type: ignore[attr-defined]
    return router


async def _proxy(service: GatewayService, path: str, request: Request) -> Response:
    authorization = request.headers.get("authorization")
    key = await service.authenticate(authorization)
    if key is None:
        return _unauthorized()
    exceeded = service.consume_quota(key)
    if exceeded is not None:
        return _rate_limited(exceeded)
    return await service.proxy(path, request, key, service.bearer_token(authorization))


def _unauthorized() -> Response:
    return openai_error(401, TYPE_INVALID_REQUEST, "missing or invalid api key")


def _rate_limited(exceeded: QuotaExceeded) -> Response:
    return openai_error(
        429,
        TYPE_RATE_LIMIT,
        "api key request quota exceeded",
        headers={"Retry-After": str(exceeded.retry_after_s)},
    )
