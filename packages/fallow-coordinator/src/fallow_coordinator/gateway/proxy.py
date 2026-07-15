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

import httpx

from fallow_coordinator.gateway.config import GatewayConfig
from fallow_coordinator.gateway.inflight import InflightHold, InflightTracker
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
class Acquired:
    """A successfully acquired upstream response.

    ``endpoint`` is the replica that actually produced the response (which, after
    a retry, differs from the first pick) so the log attributes it correctly.
    ``hold`` is present only for streaming (it must live until the body drains);
    buffered responses have already completed upstream and released their slot.
    """

    endpoint: ReplicaEndpoint
    retried: bool
    hold: InflightHold | None = None
    stream: StreamHandle | None = None
    buffered: PassThrough | None = None


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
_Outcome = PassThrough | StreamHandle | _Retry
_TryOnce = Callable[[ProxyRequest, ReplicaEndpoint], Awaitable[_Outcome]]

_RETRYABLE_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.TimeoutException)


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
        return await self._acquire(request, first, repick, self._try_buffered)

    async def acquire_stream(
        self, request: ProxyRequest, first: ReplicaEndpoint, repick: RePick
    ) -> AcquireResult:
        return await self._acquire(request, first, repick, self._try_stream)

    async def _acquire(
        self,
        request: ProxyRequest,
        first: ReplicaEndpoint,
        repick: RePick,
        try_once: _TryOnce,
    ) -> AcquireResult:
        tried: set[tuple[str, int]] = set()
        endpoint: ReplicaEndpoint | None = first
        last_5xx: tuple[ReplicaEndpoint, PassThrough] | None = None
        attempts = 0
        while endpoint is not None and attempts < _MAX_ATTEMPTS:
            attempts += 1
            hold = self._tracker.hold(endpoint.host, endpoint.port)
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
    outcome: PassThrough | StreamHandle,
    hold: InflightHold,
    endpoint: ReplicaEndpoint,
    retried: bool,
) -> Acquired:
    if isinstance(outcome, StreamHandle):
        return Acquired(endpoint=endpoint, retried=retried, hold=hold, stream=outcome)
    hold.release()  # buffered call already completed upstream
    return Acquired(endpoint=endpoint, retried=retried, buffered=outcome)


def _url(endpoint: ReplicaEndpoint, path: str) -> str:
    return f"http://{endpoint.host}:{endpoint.port}{path}"


def _media(response: httpx.Response) -> str:
    return str(response.headers.get("content-type", _DEFAULT_MEDIA))
