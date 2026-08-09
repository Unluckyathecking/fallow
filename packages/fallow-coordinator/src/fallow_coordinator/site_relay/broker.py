"""Bounded in-memory rendezvous and response relay for Site Mode."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from uuid import uuid4

MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_CHUNK_BYTES = 32 * 1024
MAX_RESPONSE_BUFFER_BYTES = 256 * 1024

_ALLOWED_PATHS = ("/v1/chat/completions", "/v1/embeddings")
_FAIL_CODES = {
    "became_active",
    "reclaimed",
    "connect_failed",
    "timeout",
    "cancelled",
    "upstream_error",
}
_TERMINAL = {"finished", "failed", "invalid"}
_DEADLINE_EXPIRED = "deadline_expired"

# Classification retained after a claim leaves the live registry so that a late
# upload can be told apart: a completed claim is a duplicate (409), a claim that
# failed, was invalidated, expired or disconnected is gone (410), and anything
# else is unknown (404). The history is bounded; evicted entries read as unknown.
_DUPLICATE = "duplicate"
_GONE = "gone"
_UNKNOWN = "unknown"
_CONFLICT = "conflict"
_TERMINAL_HISTORY = 4096


class RelayError(Exception):
    pass


class RelayRequestTooLarge(RelayError):
    pass


class RelayStateError(RelayError):
    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RelayRequest:
    method: str = "POST"
    path: str = "/v1/chat/completions"
    content_type: str = "application/json"
    body: bytes = b""


class _ResponseStream:
    """A single bounded byte stream with out-of-band termination.

    Producers block on a full buffer without holding the broker lock. Any
    terminal state releases blocked producers and is delivered only after the
    buffered prefix drains, so a slow client always sees a contiguous prefix
    followed by a clean EOF or an explicit failure reason.
    """

    def __init__(self, max_bytes: int) -> None:
        self._max_bytes = max(max_bytes, MAX_RESPONSE_CHUNK_BYTES)
        self._chunks: deque[bytes] = deque()
        self._buffered = 0
        self._terminal: str | None = None
        self.first_byte = False
        self._cond = asyncio.Condition()

    async def write(self, chunk: bytes, deadline: float) -> bool:
        """Append a chunk, blocking on a full buffer.

        Returns False if the deadline passed while waiting for room, so the
        caller can terminate the claim without letting a byte cross the deadline
        after the buffer drained. Raises if the stream terminated first.
        """
        async with self._cond:
            while (
                self._terminal is None
                and self._chunks
                and self._buffered + len(chunk) > self._max_bytes
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                try:
                    await asyncio.wait_for(self._cond.wait(), remaining)
                except TimeoutError:
                    return False
            if self._terminal is not None:
                raise RelayStateError("eof" if self._terminal == "eof" else self._terminal)
            if time.monotonic() >= deadline:
                return False
            self._chunks.append(chunk)
            self._buffered += len(chunk)
            self.first_byte = True
            self._cond.notify_all()
            return True

    async def read(self) -> tuple[bytes | None, str | None]:
        async with self._cond:
            while not self._chunks and self._terminal is None:
                await self._cond.wait()
            if self._chunks:
                chunk = self._chunks.popleft()
                self._buffered -= len(chunk)
                self._cond.notify_all()
                return chunk, None
            return None, self._terminal

    async def close(self, terminal: str) -> None:
        async with self._cond:
            if self._terminal is not None:
                return
            self._terminal = terminal
            self._cond.notify_all()


@dataclass
class _Work:
    agent_id: str
    replica_port: int
    request: RelayRequest
    deadline: float
    generation: int | None = None
    claim_id: str = field(default_factory=lambda: uuid4().hex)
    state: str = "claimed"
    status: int = 200
    content_type: str = "application/json"
    reason: str | None = None
    started: asyncio.Event = field(default_factory=asyncio.Event)
    stream: _ResponseStream = field(
        default_factory=lambda: _ResponseStream(MAX_RESPONSE_BUFFER_BYTES)
    )


@dataclass(frozen=True)
class RelayClaim:
    claim_id: str
    agent_id: str
    presence_generation: int
    replica_port: int
    request: RelayRequest
    deadline: float
    _work: _Work = field(repr=False, compare=False)


class RelayExchange:
    def __init__(self, broker: RelayBroker, work: _Work) -> None:
        self._broker, self._work = broker, work

    @property
    def claim_id(self) -> str:
        return self._work.claim_id

    @property
    def status(self) -> int:
        return self._work.status

    @property
    def content_type(self) -> str:
        return self._work.content_type

    @property
    def first_byte(self) -> bool:
        return self._work.stream.first_byte

    async def wait_response(self) -> tuple[int, str]:
        """Block until the response starts, returning its status and media type.

        Returns (status, content_type) so the gateway can preserve upstream
        media types such as text/event-stream and application/json. Permits the
        contract's single repick only when the claim fails or is invalidated
        before any response byte was buffered; once a byte has crossed the retry
        boundary the status is returned and the buffered prefix streams to
        completion, where the terminal failure surfaces as a truncation.
        """
        await self._work.started.wait()
        if self._work.state in ("failed", "invalid") and not self._work.stream.first_byte:
            raise RelayStateError(self._work.reason or "relay failure")
        return self._work.status, self._work.content_type

    async def start_response(
        self,
        agent_id: str,
        claim_id: str,
        generation: int,
        status: int = 200,
        content_type: str = "application/json",
    ) -> None:
        return await self._broker.start_response(
            agent_id, claim_id, generation, status, content_type
        )

    async def write(self, agent_id: str, claim_id: str, generation: int, chunk: bytes) -> None:
        return await self._broker.write(agent_id, claim_id, generation, chunk)

    async def finish(self, agent_id: str, claim_id: str, generation: int) -> None:
        return await self._broker.finish(agent_id, claim_id, generation)

    async def fail(self, agent_id: str, claim_id: str, generation: int, code: str) -> None:
        return await self._broker.fail(agent_id, claim_id, generation, code)

    async def aclose(self) -> None:
        await self._broker.disconnect(self._work)

    def __aiter__(self) -> RelayExchange:
        return self

    async def __anext__(self) -> bytes:
        chunk, terminal = await self._work.stream.read()
        if chunk is not None:
            return chunk
        if terminal == "eof":
            raise StopAsyncIteration
        raise RelayStateError(terminal or "relay failure")


class RelayBroker:
    def __init__(self, *, max_response_buffer: int = MAX_RESPONSE_BUFFER_BYTES) -> None:
        self._lock = asyncio.Lock()
        self._waiters: dict[str, list[tuple[asyncio.Future[_Work], int]]] = {}
        self._works: dict[str, _Work] = {}
        self._max_buffer = max_response_buffer
        self._generation: dict[str, int] = {}
        self._terminal_class: OrderedDict[str, str] = OrderedDict()

    async def offer(
        self, agent_id: str, replica_port: int, request: RelayRequest | bytes, deadline: float
    ) -> RelayExchange:
        if isinstance(request, bytes):
            request = RelayRequest(body=request)
        if len(request.body) > MAX_REQUEST_BYTES:
            raise RelayRequestTooLarge()
        if (
            request.method != "POST"
            or request.path not in _ALLOWED_PATHS
            or request.content_type != "application/json"
        ):
            raise RelayStateError("invalid request")
        async with self._lock:
            slots = self._waiters.get(agent_id, [])
            while slots and slots[0][0].done():
                slots.pop(0)
            if not slots:
                raise RelayStateError("no claimant")
            fut, generation = slots.pop(0)
            self._waiters[agent_id] = slots
            if generation < self._generation.get(agent_id, generation):
                fut.cancel()
                raise RelayStateError("stale claimant")
            work = _Work(
                agent_id,
                replica_port,
                request,
                deadline,
                generation,
                stream=_ResponseStream(self._max_buffer),
            )
            self._works[work.claim_id] = work
            fut.set_result(work)
        return RelayExchange(self, work)

    async def claim(
        self, agent_id: str, presence_generation: int, timeout: float
    ) -> RelayClaim | None:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[_Work] = loop.create_future()
        async with self._lock:
            fence = self._generation.get(agent_id, 0)
            if presence_generation < fence:
                raise RelayStateError("stale generation")
            self._generation[agent_id] = max(fence, presence_generation)
            self._waiters.setdefault(agent_id, []).append((fut, presence_generation))
        try:
            work = await asyncio.wait_for(fut, timeout)
        except (TimeoutError, asyncio.CancelledError):
            await self._abandon_waiter(agent_id, fut)
            return None
        return RelayClaim(
            work.claim_id,
            agent_id,
            presence_generation,
            work.replica_port,
            work.request,
            work.deadline,
            work,
        )

    async def _abandon_waiter(self, agent_id: str, fut: asyncio.Future[_Work]) -> None:
        """Clean up a waiter that gave up on claim.

        If offer already assigned work to this future before the claim resumed,
        that work is orphaned: no agent will process it, so terminate it and
        release its exchange instead of leaking a hung stream.
        """
        stream: _ResponseStream | None = None
        async with self._lock:
            self._waiters[agent_id] = [
                (f, g) for f, g in self._waiters.get(agent_id, []) if f is not fut
            ]
            if fut.done() and not fut.cancelled() and fut.exception() is None:
                stream = self._terminate(fut.result(), "failed", "cancelled")
            elif not fut.done():
                fut.cancel()
        if stream is not None:
            await stream.close("cancelled")

    def _check(self, agent: str, claim: str, generation: int) -> _Work:
        w = self._works.get(claim)
        if w is None:
            kind = self._terminal_class.get(claim)
            if kind == _DUPLICATE:
                raise RelayStateError("claim already completed", code=_DUPLICATE)
            if kind == _GONE:
                raise RelayStateError("claim gone", code=_GONE)
            raise RelayStateError("unknown claim", code=_UNKNOWN)
        if w.agent_id != agent:
            raise RelayStateError("wrong owner", code=_CONFLICT)
        if w.generation != generation:
            raise RelayStateError("stale generation", code=_UNKNOWN)
        return w

    def _record_terminal(self, claim_id: str, state: str) -> None:
        self._terminal_class[claim_id] = _DUPLICATE if state == "finished" else _GONE
        self._terminal_class.move_to_end(claim_id)
        while len(self._terminal_class) > _TERMINAL_HISTORY:
            self._terminal_class.popitem(last=False)

    def _terminate(self, w: _Work, state: str, reason: str | None) -> _ResponseStream | None:
        if w.state in _TERMINAL:
            return None
        w.state = state
        w.reason = reason
        self._works.pop(w.claim_id, None)
        self._record_terminal(w.claim_id, state)
        w.started.set()
        return w.stream

    def _expire_if_late(self, w: _Work) -> _ResponseStream | None:
        """Terminate live work whose deadline has passed, returning its stream.

        Returns None while the claim is still within its deadline.
        """
        if time.monotonic() >= w.deadline:
            return self._terminate(w, "failed", _DEADLINE_EXPIRED)
        return None

    async def _expired(self, stream: _ResponseStream | None) -> None:
        if stream is not None:
            await stream.close(_DEADLINE_EXPIRED)
            raise RelayStateError("deadline expired", code=_GONE)

    async def start_response(
        self,
        agent_id: str,
        claim_id: str,
        generation: int,
        status: int = 200,
        content_type: str = "application/json",
    ) -> None:
        async with self._lock:
            w = self._check(agent_id, claim_id, generation)
            late = self._expire_if_late(w)
            if late is None:
                if w.state != "claimed":
                    raise RelayStateError("response already started")
                w.status = status
                w.content_type = content_type
                w.state = "responding"
                w.started.set()
        await self._expired(late)

    async def write(self, agent_id: str, claim_id: str, generation: int, chunk: bytes) -> None:
        if len(chunk) > MAX_RESPONSE_CHUNK_BYTES:
            raise RelayStateError("chunk too large")
        stream: _ResponseStream | None = None
        deadline = 0.0
        async with self._lock:
            w = self._check(agent_id, claim_id, generation)
            late = self._expire_if_late(w)
            if late is None:
                if w.state != "responding":
                    raise RelayStateError("response not started")
                stream = w.stream
                deadline = w.deadline
        await self._expired(late)
        if not chunk or stream is None:
            return
        if not await stream.write(chunk, deadline):
            await self._deadline_terminate(claim_id)
            raise RelayStateError("deadline expired", code=_GONE)

    async def _deadline_terminate(self, claim_id: str) -> None:
        async with self._lock:
            w = self._works.get(claim_id)
            stream = self._terminate(w, "failed", _DEADLINE_EXPIRED) if w is not None else None
        if stream is not None:
            await stream.close(_DEADLINE_EXPIRED)

    async def finish(self, agent_id: str, claim_id: str, generation: int) -> None:
        stream: _ResponseStream | None = None
        async with self._lock:
            w = self._check(agent_id, claim_id, generation)
            late = self._expire_if_late(w)
            if late is None:
                if w.state != "responding":
                    raise RelayStateError("response not started")
                stream = self._terminate(w, "finished", None)
        await self._expired(late)
        if stream is not None:
            await stream.close("eof")

    async def fail(self, agent_id: str, claim_id: str, generation: int, code: str) -> None:
        if code not in _FAIL_CODES:
            raise RelayStateError("invalid failure")
        stream: _ResponseStream | None = None
        async with self._lock:
            w = self._check(agent_id, claim_id, generation)
            late = self._expire_if_late(w)
            if late is None:
                stream = self._terminate(w, "failed", code)
        await self._expired(late)
        if stream is not None:
            await stream.close(code)

    async def invalidate_agent(self, agent_id: str, newer_generation: int, reason: str) -> None:
        async with self._lock:
            self._generation[agent_id] = max(self._generation.get(agent_id, 0), newer_generation)
            streams = []
            for w in list(self._works.values()):
                if w.agent_id == agent_id and (
                    w.generation is None or w.generation < newer_generation
                ):
                    stream = self._terminate(w, "invalid", reason)
                    if stream is not None:
                        streams.append(stream)
            kept: list[tuple[asyncio.Future[_Work], int]] = []
            for f, g in self._waiters.pop(agent_id, []):
                if g < newer_generation:
                    if not f.done():
                        f.cancel()
                else:
                    kept.append((f, g))
            if kept:
                self._waiters[agent_id] = kept
        for stream in streams:
            await stream.close(reason)

    async def disconnect(self, w: _Work) -> None:
        async with self._lock:
            stream = self._terminate(w, "invalid", "client_disconnect")
        if stream is not None:
            await stream.close("client_disconnect")
