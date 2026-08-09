"""Upstream acquisition with one-shot, before-first-byte retry.

This module dials a chosen replica and, if it fails *before any byte has been
committed to the client*, retries exactly once on a different endpoint. The
"before first byte" boundary is enforced eagerly:

* Buffered path: the whole upstream response arrives before we return anything,
  so a connect error / timeout / 5xx is always pre-first-byte and retryable.
* Streaming path: we call ``client.send(stream=True)`` and read the *first* body
  chunk here (under the first-byte guard) — still before the ``StreamingResponse``
  exists — so a stalled or failing replica is retried with a correct status code.
  The open response and its raw-byte iterator are then handed to the caller, who
  drives the remaining chunks and ``aclose()``s in a ``finally``. We never wrap
  the stream in ``async with`` (that would close it before the body is sent — the
  classic premature-close trap); the :class:`InflightHold` and response lifetime
  span the whole ``StreamingResponse`` instead.

Retry never happens once the first byte is out: the caller terminates a
mid-stream failure cleanly (a truncated response), because re-issuing a POST that
already reached the backend is unsafe.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

import httpx

from fallow_coordinator.gateway.config import GatewayConfig
from fallow_coordinator.gateway.inflight import InflightHold, InflightTracker
from fallow_coordinator.gateway.protocols import SiteRoute, SiteRouteResolver
from fallow_coordinator.site_relay import (
    RelayBroker,
    RelayExchange,
    RelayRequest,
    RelayRequestTooLarge,
    RelayStateError,
)
from fallow_protocol.messages import ReplicaEndpoint

_MAX_ATTEMPTS = 2  # the original endpoint plus one retry on a different one
_SERVER_ERROR = 500
_DEFAULT_MEDIA = "application/json"
_STREAM_MEDIA = "text/event-stream"

# Given the set of already-failed ``(host, port)`` keys, pick the next endpoint.
RePick = Callable[[set[tuple[str, int]]], ReplicaEndpoint | None]


@dataclass(frozen=True)
class ProxyRequest:
    """The verbatim request the gateway replays to a replica."""

    method: str
    path: str
    body: bytes
    content_type: str


@dataclass(frozen=True)
class PassThrough:
    """A fully-buffered upstream response ready to return to the client."""

    status_code: int
    body: bytes
    media_type: str


@dataclass(frozen=True)
class StreamHandle:
    """An open streaming response: first chunk already read, rest pending."""

    response: httpx.Response
    chunks: AsyncIterator[bytes]
    first: bytes | None
    media_type: str


@dataclass(frozen=True)
class RelayStreamHandle:
    """An open Site Mode relay response: first chunk already read, rest pending.

    Mirrors :class:`StreamHandle` for the relay path — the buffered prefix and
    raw SSE bytes stream straight off the :class:`RelayExchange` with no reframe.
    """

    exchange: RelayExchange
    first: bytes | None
    status_code: int
    media_type: str


@dataclass(frozen=True)
class Acquired:
    """A successfully acquired upstream response.

    ``endpoint`` is the replica that actually produced the response (which, after
    a retry, differs from the first pick) so the log attributes it correctly.
    ``hold`` is present only for streaming (it must live until the body drains);
    buffered responses have already completed upstream and released their slot.
    ``relay_stream`` is the Site Mode analogue of ``stream`` for a relayed agent.
    """

    endpoint: ReplicaEndpoint
    retried: bool
    hold: InflightHold | None = None
    stream: StreamHandle | None = None
    buffered: PassThrough | None = None
    relay_stream: RelayStreamHandle | None = None


@dataclass(frozen=True)
class NoUpstream:
    """Every attempt failed to reach a replica (connect/timeout)."""

    retried: bool


@dataclass(frozen=True)
class _Retry:
    """Internal signal: this attempt failed before any byte reached the client.

    ``passthrough`` carries a backend 5xx (body already read) so it can be
    forwarded verbatim if the retry budget is exhausted; ``None`` marks an
    unreachable replica (connect error / timeout) with nothing to forward.
    """

    passthrough: PassThrough | None


AcquireResult = Acquired | NoUpstream
_Outcome = PassThrough | StreamHandle | RelayStreamHandle | _Retry
_TryOnce = Callable[[ProxyRequest, ReplicaEndpoint], Awaitable[_Outcome]]

_RETRYABLE_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.TimeoutException)


class ReplicaTransport(Protocol):
    """The acquire surface every routing transport exposes to the gateway.

    ``UpstreamProxy`` is the direct HTTP implementation; ``SiteRelayTransport``
    serves a relayed Site Mode agent. Both honour the same before-first-byte
    retry contract, so the gateway service drives either through one seam.
    """

    async def acquire_buffered(
        self, request: ProxyRequest, first: ReplicaEndpoint, repick: RePick
    ) -> AcquireResult: ...

    async def acquire_stream(
        self, request: ProxyRequest, first: ReplicaEndpoint, repick: RePick
    ) -> AcquireResult: ...


async def _run_attempts(
    request: ProxyRequest,
    first: ReplicaEndpoint,
    repick: RePick,
    try_once: _TryOnce,
    tracker: InflightTracker,
) -> AcquireResult:
    """The shared before-first-byte retry loop: the first pick plus one repick.

    Every transport runs the same loop so a mixed direct/relay pool preserves one
    repick budget and one first-byte boundary; a byte already committed to the
    client is never replayed.
    """
    tried: set[tuple[str, int]] = set()
    endpoint: ReplicaEndpoint | None = first
    last_5xx: tuple[ReplicaEndpoint, PassThrough] | None = None
    attempts = 0
    while endpoint is not None and attempts < _MAX_ATTEMPTS:
        attempts += 1
        hold = tracker.hold(endpoint.host, endpoint.port)
        outcome = await try_once(request, endpoint)
        if not isinstance(outcome, _Retry):
            return _as_success(outcome, hold, endpoint, retried=attempts > 1)
        hold.release()
        if outcome.passthrough is not None:
            last_5xx = (endpoint, outcome.passthrough)
        tried.add((endpoint.host, endpoint.port))
        endpoint = repick(tried) if attempts < _MAX_ATTEMPTS else None
    retried = attempts > 1
    if last_5xx is not None:
        served_by, passthrough = last_5xx
        return Acquired(endpoint=served_by, retried=retried, buffered=passthrough)
    return NoUpstream(retried=retried)


class UpstreamProxy:
    """Dials replicas and enforces the before-first-byte retry policy."""

    def __init__(
        self, client: httpx.AsyncClient, config: GatewayConfig, tracker: InflightTracker
    ) -> None:
        self._client = client
        self._config = config
        self._tracker = tracker

    async def acquire_buffered(
        self, request: ProxyRequest, first: ReplicaEndpoint, repick: RePick
    ) -> AcquireResult:
        return await _run_attempts(request, first, repick, self._try_buffered, self._tracker)

    async def acquire_stream(
        self, request: ProxyRequest, first: ReplicaEndpoint, repick: RePick
    ) -> AcquireResult:
        return await _run_attempts(request, first, repick, self._try_stream, self._tracker)

    async def _try_buffered(self, request: ProxyRequest, endpoint: ReplicaEndpoint) -> _Outcome:
        try:
            response = await self._client.request(
                request.method,
                _url(endpoint, request.path),
                content=request.body,
                headers={"content-type": request.content_type},
                timeout=self._config.httpx_timeout(),
            )
        except _RETRYABLE_ERRORS:
            return _Retry(passthrough=None)
        passthrough = PassThrough(response.status_code, response.content, _media(response))
        if response.status_code >= _SERVER_ERROR:
            return _Retry(passthrough=passthrough)
        return passthrough

    async def _try_stream(self, request: ProxyRequest, endpoint: ReplicaEndpoint) -> _Outcome:
        built = self._client.build_request(
            request.method,
            _url(endpoint, request.path),
            content=request.body,
            headers={"content-type": request.content_type},
            timeout=self._config.httpx_timeout(),
        )
        try:
            response = await self._client.send(built, stream=True)
        except _RETRYABLE_ERRORS:
            return _Retry(passthrough=None)
        if response.status_code >= _SERVER_ERROR:
            body = await response.aread()
            await response.aclose()
            return _Retry(passthrough=PassThrough(response.status_code, body, _media(response)))
        return await self._first_chunk(response)

    async def _first_chunk(self, response: httpx.Response) -> _Outcome:
        chunks = response.aiter_raw()
        try:
            first: bytes | None = await asyncio.wait_for(
                anext(chunks), self._config.first_byte_timeout_s
            )
        except StopAsyncIteration:
            first = None  # empty (but valid) body — nothing to retry
        except (TimeoutError, httpx.TimeoutException, httpx.HTTPError):
            await response.aclose()
            return _Retry(passthrough=None)
        media = response.headers.get("content-type", _STREAM_MEDIA)
        return StreamHandle(response=response, chunks=chunks, first=first, media_type=media)


def _as_success(
    outcome: PassThrough | StreamHandle | RelayStreamHandle,
    hold: InflightHold,
    endpoint: ReplicaEndpoint,
    retried: bool,
) -> Acquired:
    if isinstance(outcome, StreamHandle):
        return Acquired(endpoint=endpoint, retried=retried, hold=hold, stream=outcome)
    if isinstance(outcome, RelayStreamHandle):
        return Acquired(endpoint=endpoint, retried=retried, hold=hold, relay_stream=outcome)
    hold.release()  # buffered call already completed upstream
    return Acquired(endpoint=endpoint, retried=retried, buffered=outcome)


def _url(endpoint: ReplicaEndpoint, path: str) -> str:
    return f"http://{endpoint.host}:{endpoint.port}{path}"


def _media(response: httpx.Response) -> str:
    return str(response.headers.get("content-type", _DEFAULT_MEDIA))


def _normalize_json_media(content_type: str) -> str:
    """Collapse a parameterised JSON media type to the bare ``application/json``.

    The relay broker matches the media type exactly, but a client may send
    ``application/json; charset=utf-8``. Strip the parameters for a JSON body so a
    valid request relays instead of being rejected; anything else passes through
    for the broker to reject.
    """
    base = content_type.split(";", 1)[0].strip().lower()
    return _DEFAULT_MEDIA if base == _DEFAULT_MEDIA else content_type


class SiteRelayTransport:
    """Serves a Site Mode agent through the in-memory relay, never its host.

    ``offer`` only proceeds when the agent already holds a claim, so an agent that
    is not waiting simply fails the attempt and the loop repicks — a disconnected
    site agent can never fall back to a direct dial on its registered address.
    """

    def __init__(
        self,
        broker: RelayBroker,
        *,
        deadline_s: float,
        monotonic: Callable[[], float],
        first_byte_timeout_s: float,
        take_nonretryable: Callable[[str], bool] | None = None,
    ) -> None:
        self._broker = broker
        self._deadline_s = deadline_s
        self._monotonic = monotonic
        self._first_byte_timeout_s = first_byte_timeout_s
        # Consumes an agent's "retryable: false" failure flag for a claim id, so a
        # deterministic non-retryable failure surfaces an error instead of a repick.
        self._take_nonretryable = take_nonretryable

    async def _offer(
        self, request: ProxyRequest, endpoint: ReplicaEndpoint
    ) -> RelayExchange | None:
        deadline = self._monotonic() + self._deadline_s
        relay_request = RelayRequest(
            method=request.method,
            path=request.path,
            content_type=_normalize_json_media(request.content_type),
            body=request.body,
        )
        try:
            return await self._broker.offer(
                endpoint.agent_id, endpoint.port, relay_request, deadline
            )
        except (RelayStateError, RelayRequestTooLarge):
            return None  # no claimant / stale / invalid / oversize: repick, never dial

    def _on_failure(self, exchange: RelayExchange) -> _Outcome:
        """Map a pre-first-byte relay failure to a repick or a terminal error.

        An agent that reported ``retryable: false`` short-circuits to a 502 so the
        request is not replayed on another agent; every other pre-byte failure
        keeps the contract's single repick.
        """
        if self._take_nonretryable is not None and self._take_nonretryable(exchange.claim_id):
            return PassThrough(_SERVER_ERROR, b"", _DEFAULT_MEDIA)
        return _Retry(passthrough=None)

    async def try_buffered(self, request: ProxyRequest, endpoint: ReplicaEndpoint) -> _Outcome:
        exchange = await self._offer(request, endpoint)
        if exchange is None:
            return _Retry(passthrough=None)
        try:
            status, content_type, body = await asyncio.wait_for(
                _drain_relay(exchange), self._deadline_s
            )
        except (RelayStateError, TimeoutError):
            await exchange.aclose()
            return self._on_failure(exchange)  # pre-client-byte failure
        if status >= _SERVER_ERROR:
            # Match the direct path: a 5xx before any client byte spends the one
            # repick and, if the budget runs out, is forwarded verbatim.
            return _Retry(passthrough=PassThrough(status, bytes(body), content_type))
        return PassThrough(status, bytes(body), content_type)

    async def try_stream(self, request: ProxyRequest, endpoint: ReplicaEndpoint) -> _Outcome:
        exchange = await self._offer(request, endpoint)
        if exchange is None:
            return _Retry(passthrough=None)
        try:
            status, content_type, first = await asyncio.wait_for(
                _relay_first_byte(exchange), self._first_byte_timeout_s
            )
        except (RelayStateError, TimeoutError):
            await exchange.aclose()
            return self._on_failure(exchange)  # failed before the first byte
        if status >= _SERVER_ERROR:
            body = bytearray(first or b"")
            try:
                async for chunk in exchange:
                    body.extend(chunk)
            except RelayStateError:
                pass
            await exchange.aclose()
            return _Retry(passthrough=PassThrough(status, bytes(body), content_type))
        return RelayStreamHandle(
            exchange=exchange, first=first, status_code=status, media_type=content_type
        )


class SiteAwareTransport:
    """Selects the direct or relay path per attempt from the persisted transport.

    Runs the one shared retry loop, so ``direct`` agents stay byte-for-byte on the
    HTTP proxy and ``site_relay`` agents route through the broker; a repick that
    crosses transports keeps the single before-first-byte budget.
    """

    def __init__(
        self,
        direct: UpstreamProxy,
        relay: SiteRelayTransport,
        resolve: SiteRouteResolver,
        tracker: InflightTracker,
    ) -> None:
        self._direct = direct
        self._relay = relay
        self._resolve = resolve
        self._tracker = tracker

    async def acquire_buffered(
        self, request: ProxyRequest, first: ReplicaEndpoint, repick: RePick
    ) -> AcquireResult:
        return await _run_attempts(request, first, repick, self._buffered, self._tracker)

    async def acquire_stream(
        self, request: ProxyRequest, first: ReplicaEndpoint, repick: RePick
    ) -> AcquireResult:
        return await _run_attempts(request, first, repick, self._stream, self._tracker)

    async def _route(self, agent_id: str) -> SiteRoute | None:
        return await self._resolve(agent_id)

    async def _buffered(self, request: ProxyRequest, endpoint: ReplicaEndpoint) -> _Outcome:
        if await self._route(endpoint.agent_id) is not None:
            return await self._relay.try_buffered(request, endpoint)
        return await self._direct._try_buffered(request, endpoint)

    async def _stream(self, request: ProxyRequest, endpoint: ReplicaEndpoint) -> _Outcome:
        if await self._route(endpoint.agent_id) is not None:
            return await self._relay.try_stream(request, endpoint)
        return await self._direct._try_stream(request, endpoint)


async def _relay_first_byte(exchange: RelayExchange) -> tuple[int, str, bytes | None]:
    """Wait for the relay response, then read its first byte (``None`` when empty)."""
    status, content_type = await exchange.wait_response()
    try:
        first: bytes | None = await anext(aiter(exchange))
    except StopAsyncIteration:
        first = None
    return status, content_type, first


async def _drain_relay(exchange: RelayExchange) -> tuple[int, str, bytes]:
    """Wait for the relay response and buffer its whole body."""
    status, content_type = await exchange.wait_response()
    body = bytearray()
    async for chunk in exchange:
        body.extend(chunk)
    return status, content_type, bytes(body)


async def relay_stream_body(
    handle: RelayStreamHandle,
    inter_chunk_timeout_s: float,
    hold: InflightHold,
    finalize: Callable[[], None],
) -> AsyncIterator[bytes]:
    """Yield the relayed body verbatim, then release the slot and finalize once.

    Raw SSE bytes pass through untouched. Each subsequent chunk is bounded by the
    inter-chunk timeout; a stalled or failing relay ends the iterator (a truncated
    response) and is never replayed — the first byte is already out.
    """
    try:
        if handle.first is not None:
            yield handle.first
        chunks = aiter(handle.exchange)
        while True:
            try:
                chunk = await asyncio.wait_for(anext(chunks), inter_chunk_timeout_s)
            except StopAsyncIteration:
                return
            except (RelayStateError, TimeoutError):
                return  # first byte already sent: terminate cleanly, never retry
            yield chunk
    finally:
        await handle.exchange.aclose()
        hold.release()
        finalize()
