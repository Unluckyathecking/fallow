"""Gateway orchestration: auth, model resolution, routing, proxying, logging.

Ties the pieces together for one interactive request:

1. authenticate the API key and enforce its per-key model allowlist;
2. parse the body once, resolve the model, 404 the unknown;
3. enrich replica endpoints with a *live* inflight count, then delegate the
   choice to the injected ``pick_replica`` (the scheduler policy);
4. proxy verbatim — buffered or streaming — with before-first-byte retry;
5. emit exactly one :class:`GatewayLogEntry`, whose ``status`` feeds the study's
   served-vs-shed metric.
"""

from collections.abc import Callable, Sequence
from datetime import datetime

from fastapi import Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from fallow_coordinator.gateway.bodyparse import ParsedBody, parse_body
from fallow_coordinator.gateway.errors import (
    TYPE_INVALID_REQUEST,
    TYPE_MODEL_NOT_FOUND,
    TYPE_NO_REPLICA,
    TYPE_UPSTREAM,
    openai_error,
)
from fallow_coordinator.gateway.inflight import InflightTracker
from fallow_coordinator.gateway.logentry import GatewayLogEntry, LogStatus
from fallow_coordinator.gateway.protocols import GatewayRegistry, PickReplica, RequestLog
from fallow_coordinator.gateway.proxy import (
    Acquired,
    NoUpstream,
    ProxyRequest,
    UpstreamProxy,
)
from fallow_coordinator.gateway.quota import QuotaExceeded, QuotaManager
from fallow_coordinator.gateway.streaming import stream_body
from fallow_coordinator.registry import ApiKeyInfo
from fallow_protocol.messages import ReplicaEndpoint

_DEFAULT_CONTENT_TYPE = "application/json"
_MODEL_OWNER = "fallow"


class GatewayService:
    """Stateless-per-request coordinator between registry, policy, and replicas."""

    def __init__(
        self,
        registry: GatewayRegistry,
        pick_replica: PickReplica,
        proxy: UpstreamProxy,
        request_log: RequestLog,
        now: Callable[[], datetime],
        tracker: InflightTracker,
        inter_chunk_timeout_s: float,
        quotas: QuotaManager | None = None,
    ) -> None:
        self._registry = registry
        self._pick = pick_replica
        self._proxy = proxy
        self._log = request_log
        self._now = now
        self._tracker = tracker
        self._inter_chunk_timeout_s = inter_chunk_timeout_s
        self._quotas = quotas

    async def authenticate(self, authorization: str | None) -> ApiKeyInfo | None:
        token = _extract_bearer(authorization)
        if token is None:
            return None
        return await self._registry.authenticate_api_key(token)

    def consume_quota(self, key: ApiKeyInfo) -> QuotaExceeded | None:
        if self._quotas is None:
            return None
        return self._quotas.consume(key)

    async def list_models(self, key: ApiKeyInfo) -> JSONResponse:
        manifests = await self._registry.list_models()
        created = int(self._now().timestamp())
        data = [
            {"id": m.model_id, "object": "model", "created": created, "owned_by": _MODEL_OWNER}
            for m in manifests
            if _allows(key, m.model_id)
        ]
        return JSONResponse({"object": "list", "data": data})

    async def proxy(self, path: str, request: Request, key: ApiKeyInfo) -> Response:
        t_submit = self._now()
        parsed = parse_body(await request.body())
        if parsed is None or parsed.model is None:
            return openai_error(
                400, TYPE_INVALID_REQUEST, "request body must be JSON with a 'model' field"
            )
        model = parsed.model
        if not _allows(key, model):
            return openai_error(
                403, TYPE_INVALID_REQUEST, f"api key not permitted to use model '{model}'"
            )
        known = {m.model_id for m in await self._registry.list_models()}
        if model not in known:
            return openai_error(404, TYPE_MODEL_NOT_FOUND, f"model '{model}' does not exist")
        return await self._route(path, request, key, parsed, model, t_submit)

    async def _route(
        self,
        path: str,
        request: Request,
        key: ApiKeyInfo,
        parsed: ParsedBody,
        model: str,
        t_submit: datetime,
    ) -> Response:
        endpoints = await self._registry.replica_endpoints(model, self._now())
        enriched = self._enrich(endpoints)
        chosen = self._pick(model, enriched)
        if chosen is None:
            self._record(key, model, parsed, t_submit, None, LogStatus.SHED, None, False)
            return openai_error(503, TYPE_NO_REPLICA, f"no replica available for model '{model}'")
        proxy_request = ProxyRequest(
            method=request.method,
            path=path,
            body=await request.body(),
            content_type=request.headers.get("content-type", _DEFAULT_CONTENT_TYPE),
        )
        repick = _make_repick(self._pick, model, enriched)
        if parsed.stream:
            result = await self._proxy.acquire_stream(proxy_request, chosen, repick)
        else:
            result = await self._proxy.acquire_buffered(proxy_request, chosen, repick)
        return self._respond(result, key, model, parsed, t_submit)

    def _respond(
        self,
        result: Acquired | NoUpstream,
        key: ApiKeyInfo,
        model: str,
        parsed: ParsedBody,
        t_submit: datetime,
    ) -> Response:
        if isinstance(result, NoUpstream):
            self._record(key, model, parsed, t_submit, None, LogStatus.ERROR, None, result.retried)
            return openai_error(502, TYPE_UPSTREAM, "no replica could serve the request")
        if result.stream is not None and result.hold is not None:
            return self._stream(result, key, model, parsed, t_submit)
        return self._buffered(result, key, model, parsed, t_submit)

    def _buffered(
        self,
        result: Acquired,
        key: ApiKeyInfo,
        model: str,
        parsed: ParsedBody,
        t_submit: datetime,
    ) -> Response:
        assert result.buffered is not None  # buffered path invariant
        served = result.buffered
        now = self._now()
        self._record(
            key,
            model,
            parsed,
            t_submit,
            result.endpoint.agent_id,
            LogStatus.SERVED,
            now,
            result.retried,
        )
        return Response(
            content=served.body, status_code=served.status_code, media_type=served.media_type
        )

    def _stream(
        self,
        result: Acquired,
        key: ApiKeyInfo,
        model: str,
        parsed: ParsedBody,
        t_submit: datetime,
    ) -> StreamingResponse:
        assert result.stream is not None and result.hold is not None  # stream path invariant
        handle = result.stream
        t_first = self._now()
        agent_id = result.endpoint.agent_id

        def finalize() -> None:
            self._record(
                key,
                model,
                parsed,
                t_submit,
                agent_id,
                LogStatus.SERVED,
                t_first,
                result.retried,
            )

        body = stream_body(handle, self._inter_chunk_timeout_s, result.hold, finalize)
        return StreamingResponse(
            body, status_code=handle.response.status_code, media_type=handle.media_type
        )

    def _enrich(self, endpoints: Sequence[ReplicaEndpoint]) -> tuple[ReplicaEndpoint, ...]:
        return tuple(
            endpoint.model_copy(
                update={"inflight": self._tracker.count(endpoint.host, endpoint.port)}
            )
            for endpoint in endpoints
        )

    def _record(
        self,
        key: ApiKeyInfo,
        model: str,
        parsed: ParsedBody,
        t_submit: datetime,
        agent_id: str | None,
        status: LogStatus,
        t_first: datetime | None,
        retried: bool,
    ) -> None:
        self._log.log(
            GatewayLogEntry(
                client_key_name=key.name,
                model_id=model,
                agent_id=agent_id,
                t_submit=t_submit,
                t_first_byte=t_first,
                t_done=self._now(),
                status=status,
                retried=retried,
                prompt_chars=parsed.prompt_chars,
            )
        )


def _make_repick(
    pick: PickReplica, model: str, enriched: Sequence[ReplicaEndpoint]
) -> Callable[[set[tuple[str, int]]], ReplicaEndpoint | None]:
    def repick(tried: set[tuple[str, int]]) -> ReplicaEndpoint | None:
        remaining = [e for e in enriched if (e.host, e.port) not in tried]
        return pick(model, remaining)

    return repick


def _allows(key: ApiKeyInfo, model_id: str) -> bool:
    return key.model_allowlist is None or model_id in key.model_allowlist


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()
