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
from dataclasses import dataclass
from datetime import datetime

from fastapi import Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from fallow_coordinator.gateway.admission import (
    AdmissionCancelled,
    AdmissionQueue,
    AdmissionStatus,
)
from fallow_coordinator.gateway.affinity import AffinityMap
from fallow_coordinator.gateway.bodyparse import ParsedBody, parse_body
from fallow_coordinator.gateway.errors import (
    TYPE_INVALID_REQUEST,
    TYPE_MODEL_NOT_FOUND,
    TYPE_NO_REPLICA,
    TYPE_UPSTREAM,
    openai_error,
)
from fallow_coordinator.gateway.inflight import InflightTracker
from fallow_coordinator.gateway.logentry import AffinityState, GatewayLogEntry, LogStatus
from fallow_coordinator.gateway.protocols import GatewayRegistry, PickReplica, RequestLog
from fallow_coordinator.gateway.proxy import (
    Acquired,
    NoUpstream,
    ProxyRequest,
    UpstreamProxy,
)
from fallow_coordinator.gateway.session import derive_session_key
from fallow_coordinator.gateway.streaming import stream_body
from fallow_coordinator.registry import ApiKeyInfo
from fallow_protocol.messages import ReplicaEndpoint

_DEFAULT_CONTENT_TYPE = "application/json"
_MODEL_OWNER = "fallow"


@dataclass(frozen=True)
class _RouteChoice:
    endpoint: ReplicaEndpoint
    candidates: tuple[ReplicaEndpoint, ...]
    affinity: AffinityState


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
        admission: AdmissionQueue,
        affinity: AffinityMap,
    ) -> None:
        self._registry = registry
        self._pick = pick_replica
        self._proxy = proxy
        self._log = request_log
        self._now = now
        self._tracker = tracker
        self._inter_chunk_timeout_s = inter_chunk_timeout_s
        self._admission = admission
        self._affinity = affinity

    async def authenticate(self, authorization: str | None) -> ApiKeyInfo | None:
        token = _extract_bearer(authorization)
        if token is None:
            return None
        return await self._registry.authenticate_api_key(token)

    @staticmethod
    def bearer_token(authorization: str | None) -> str:
        """Return the already-authenticated bearer for opaque key derivation."""
        token = _extract_bearer(authorization)
        assert token is not None
        return token

    async def list_models(self, key: ApiKeyInfo) -> JSONResponse:
        manifests = await self._registry.list_models()
        created = int(self._now().timestamp())
        data = [
            {"id": m.model_id, "object": "model", "created": created, "owned_by": _MODEL_OWNER}
            for m in manifests
            if _allows(key, m.model_id)
        ]
        return JSONResponse({"object": "list", "data": data})

    async def proxy(self, path: str, request: Request, key: ApiKeyInfo, bearer: str) -> Response:
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
        session_key = derive_session_key(
            model,
            request.headers.get("x-fallow-session"),
            bearer,
            parsed,
        )
        return await self._route(path, request, key, parsed, model, t_submit, session_key)

    async def _route(
        self,
        path: str,
        request: Request,
        key: ApiKeyInfo,
        parsed: ParsedBody,
        model: str,
        t_submit: datetime,
        session_key: str | None,
    ) -> Response:
        affinity = AffinityState.NONE

        async def probe() -> _RouteChoice | None:
            nonlocal affinity
            choice, affinity = await self._resolve(model, session_key, preserve_missing=True)
            return choice

        try:
            admitted = await self._admission.wait(model, probe)
        except AdmissionCancelled as cancelled:
            self._record(
                key,
                model,
                parsed,
                t_submit,
                None,
                LogStatus.CANCELLED,
                None,
                False,
                cancelled.waited_ms,
                affinity,
            )
            raise
        waited_ms = admitted.waited_ms
        choice = admitted.value
        if choice is not None:
            affinity = choice.affinity
        if choice is None:
            if session_key is not None and admitted.status is AdmissionStatus.TIMEOUT:
                self._affinity.forget(session_key)
            self._record(
                key,
                model,
                parsed,
                t_submit,
                None,
                LogStatus.SHED,
                None,
                False,
                waited_ms,
                affinity,
            )
            return openai_error(503, TYPE_NO_REPLICA, f"no replica available for model '{model}'")
        proxy_request = ProxyRequest(
            method=request.method,
            path=path,
            body=await request.body(),
            content_type=request.headers.get("content-type", _DEFAULT_CONTENT_TYPE),
        )
        repick = _make_repick(self._pick, model, choice.candidates)
        if parsed.stream:
            result = await self._proxy.acquire_stream(proxy_request, choice.endpoint, repick)
        else:
            result = await self._proxy.acquire_buffered(proxy_request, choice.endpoint, repick)
        if session_key is not None:
            if isinstance(result, Acquired):
                self._affinity.remember(session_key, result.endpoint)
            else:
                self._affinity.forget(session_key)
        return self._respond(result, key, model, parsed, t_submit, waited_ms, affinity)

    async def _resolve(
        self, model: str, session_key: str | None, *, preserve_missing: bool = False
    ) -> tuple[_RouteChoice | None, AffinityState]:
        endpoints = await self._registry.replica_endpoints(model, self._now())
        enriched = self._enrich(endpoints)
        decision = self._affinity.resolve(
            session_key,
            enriched,
            lambda candidates: self._pick(model, candidates),
            preserve_missing=preserve_missing,
        )
        if decision.endpoint is None:
            return None, decision.state
        return _RouteChoice(decision.endpoint, enriched, decision.state), decision.state

    def _respond(
        self,
        result: Acquired | NoUpstream,
        key: ApiKeyInfo,
        model: str,
        parsed: ParsedBody,
        t_submit: datetime,
        waited_ms: int,
        affinity: AffinityState,
    ) -> Response:
        if isinstance(result, NoUpstream):
            self._record(
                key,
                model,
                parsed,
                t_submit,
                None,
                LogStatus.ERROR,
                None,
                result.retried,
                waited_ms,
                affinity,
            )
            return openai_error(502, TYPE_UPSTREAM, "no replica could serve the request")
        if result.stream is not None and result.hold is not None:
            return self._stream(result, key, model, parsed, t_submit, waited_ms, affinity)
        return self._buffered(result, key, model, parsed, t_submit, waited_ms, affinity)

    def _buffered(
        self,
        result: Acquired,
        key: ApiKeyInfo,
        model: str,
        parsed: ParsedBody,
        t_submit: datetime,
        waited_ms: int,
        affinity: AffinityState,
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
            waited_ms,
            affinity,
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
        waited_ms: int,
        affinity: AffinityState,
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
                waited_ms,
                affinity,
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
        waited_ms: int,
        affinity: AffinityState,
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
                waited_ms=waited_ms,
                affinity=affinity,
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
