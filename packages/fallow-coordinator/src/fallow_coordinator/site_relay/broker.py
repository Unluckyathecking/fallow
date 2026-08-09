"""Bounded in-memory rendezvous and response relay for Site Mode."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from uuid import uuid4

MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_CHUNK_BYTES = 32 * 1024
MAX_RESPONSE_BUFFER_BYTES = 256 * 1024


class RelayError(Exception):
    pass


class RelayRequestTooLarge(RelayError):
    pass


class RelayStateError(RelayError):
    pass


@dataclass(frozen=True)
class RelayRequest:
    method: str = "POST"
    path: str = "/v1/chat/completions"
    content_type: str = "application/json"
    body: bytes = b""


@dataclass
class _Work:
    agent_id: str
    replica_port: int
    request: RelayRequest
    deadline: float
    generation: int | None = None
    claim_id: str = field(default_factory=lambda: uuid4().hex)
    first_byte: bool = False
    state: str = "queued"
    queue: asyncio.Queue[bytes | None] = field(
        default_factory=lambda: asyncio.Queue(
            maxsize=MAX_RESPONSE_BUFFER_BYTES // MAX_RESPONSE_CHUNK_BYTES
        )
    )
    buffered: int = 0
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    reason: str | None = None


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

    async def start_response(
        self, agent_id: str, claim_id: str, generation: int, status: int = 200
    ) -> None:
        return await self._broker.start_response(agent_id, claim_id, generation, status)

    async def write(self, agent_id: str, claim_id: str, generation: int, chunk: bytes) -> None:
        return await self._broker.write(agent_id, claim_id, generation, chunk)

    async def finish(self, agent_id: str, claim_id: str, generation: int) -> None:
        return await self._broker.finish(agent_id, claim_id, generation)

    async def fail(self, agent_id: str, claim_id: str, generation: int, code: str) -> None:
        return await self._broker.fail(agent_id, claim_id, generation, code)

    async def aclose(self) -> None:
        self._broker.disconnect(self._work)

    def __aiter__(self) -> RelayExchange:
        return self

    async def __anext__(self) -> bytes:
        item = await self._work.queue.get()
        if item is None:
            raise StopAsyncIteration
        self._work.buffered = max(0, self._work.buffered - len(item))
        return item


class RelayBroker:
    def __init__(self, *, max_response_buffer: int = MAX_RESPONSE_BUFFER_BYTES) -> None:
        self._lock = asyncio.Lock()
        self._waiters: dict[str, list[tuple[asyncio.Future[_Work], int]]] = {}
        self._queued: dict[str, list[_Work]] = {}
        self._works: dict[str, _Work] = {}
        self._max_buffer = max_response_buffer
        self._generation: dict[str, int] = {}

    async def offer(
        self, agent_id: str, replica_port: int, request: RelayRequest | bytes, deadline: float
    ) -> RelayExchange:
        if isinstance(request, bytes):
            request = RelayRequest(body=request)
        if len(request.body) > MAX_REQUEST_BYTES:
            raise RelayRequestTooLarge()
        if (
            request.method != "POST"
            or request.path not in ("/v1/chat/completions", "/v1/embeddings")
            or request.content_type != "application/json"
        ):
            raise RelayStateError("invalid request")
        async with self._lock:
            slots = self._waiters.get(agent_id, [])
            if not slots:
                raise RelayStateError("no claimant")
            fut, generation = slots.pop(0)
            self._waiters[agent_id] = slots
            work = _Work(agent_id, replica_port, request, deadline, generation)
            work.state = "claimed"
            self._works[work.claim_id] = work
            fut.set_result(work)
        return RelayExchange(self, work)

    async def claim(
        self, agent_id: str, presence_generation: int, timeout: float
    ) -> RelayClaim | None:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[_Work] = loop.create_future()
        async with self._lock:
            self._generation[agent_id] = max(self._generation.get(agent_id, 0), presence_generation)
            self._waiters.setdefault(agent_id, []).append((fut, presence_generation))
        try:
            work = await asyncio.wait_for(fut, timeout)
        except (TimeoutError, asyncio.CancelledError):
            if not fut.done():
                fut.cancel()
            async with self._lock:
                self._waiters[agent_id] = [
                    (f, g) for f, g in self._waiters.get(agent_id, []) if f is not fut
                ]
            return None
        work.generation = presence_generation
        return RelayClaim(
            work.claim_id,
            agent_id,
            presence_generation,
            work.replica_port,
            work.request,
            work.deadline,
            work,
        )

    def _check(self, agent: str, claim: str, generation: int) -> _Work:
        w = self._works.get(claim)
        if w is None or w.agent_id != agent or w.generation != generation:
            raise RelayStateError("unknown or stale claim")
        if w.state in ("finished", "failed", "invalid"):
            raise RelayStateError("claim already complete")
        return w

    async def start_response(
        self, agent_id: str, claim_id: str, generation: int, status: int = 200
    ) -> None:
        async with self._lock:
            w = self._check(agent_id, claim_id, generation)
            w.state = "responding"

    async def write(self, agent_id: str, claim_id: str, generation: int, chunk: bytes) -> None:
        if len(chunk) > MAX_RESPONSE_CHUNK_BYTES:
            raise RelayStateError("chunk too large")
        w = self._check(agent_id, claim_id, generation)
        if not chunk:
            return
        w.first_byte = True
        await w.queue.put(chunk)

    async def finish(self, agent_id: str, claim_id: str, generation: int) -> None:
        async with self._lock:
            w = self._check(agent_id, claim_id, generation)
            w.state = "finished"
            await w.queue.put(None)
            w.wake.set()

    async def fail(self, agent_id: str, claim_id: str, generation: int, code: str) -> None:
        if code not in {
            "became_active",
            "reclaimed",
            "connect_failed",
            "timeout",
            "cancelled",
            "upstream_error",
        }:
            raise RelayStateError("invalid failure")
        async with self._lock:
            w = self._check(agent_id, claim_id, generation)
            w.state = "failed"
            w.reason = code
            await w.queue.put(None)
            w.wake.set()

    def _invalidate(self, w: _Work, reason: str) -> None:
        if w.state in ("finished", "failed", "invalid"):
            return
        w.state = "invalid"
        w.reason = reason
        w.wake.set()
        self._works.pop(w.claim_id, None)
        w.queue.put_nowait(None)

    async def invalidate_agent(self, agent_id: str, newer_generation: int, reason: str) -> None:
        async with self._lock:
            self._generation[agent_id] = max(self._generation.get(agent_id, 0), newer_generation)
            for w in list(self._works.values()):
                if w.agent_id == agent_id and (
                    w.generation is None or w.generation < newer_generation
                ):
                    self._invalidate(w, reason)
            for f, _g in self._waiters.pop(agent_id, []):
                if not f.done():
                    f.cancel()

    def disconnect(self, w: _Work) -> None:
        self._invalidate(w, "client_disconnect")
