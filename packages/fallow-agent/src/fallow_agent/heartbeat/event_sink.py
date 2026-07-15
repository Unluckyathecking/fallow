"""``HttpEventSink``: non-blocking, durable agent event shipping.

Implements :class:`fallow_protocol.interfaces.EventSink`. ``emit`` is called
from the synchronous preemption poll thread and must NEVER block it, so it only
enqueues onto an unbounded, thread-safe :class:`queue.SimpleQueue` and (if a
loop is running) schedules a wakeup. A background asyncio task drains the queue:
for each event it first appends one JSON line to a local ``events.jsonl`` (the
durable copy) and then best-effort POSTs to the coordinator, dropping the push
after ``max_push_attempts`` failures.

Cross-thread handoff (see ADR 009): a stdlib janus-style pattern built on
``loop.call_soon_threadsafe``. ``emit`` (any thread) puts on the SimpleQueue and
calls ``call_soon_threadsafe(wake.set)``; the sender awaits ``wake`` and drains.
No ``run_coroutine_threadsafe`` future is created per event, so ``emit`` costs a
lock-free enqueue plus one thread-safe callback schedule — never a round-trip.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import queue
from collections.abc import Awaitable, Callable
from pathlib import Path

from fallow_agent.heartbeat.client import CoordinatorClient
from fallow_agent.heartbeat.config import EventSinkConfig
from fallow_agent.heartbeat.errors import CoordinatorAuthError, CoordinatorError
from fallow_protocol.interfaces import EventSink
from fallow_protocol.messages import AgentEvent

logger = logging.getLogger(__name__)

SleepFn = Callable[[float], Awaitable[None]]


class HttpEventSink(EventSink):
    """Buffer-and-ship event sink with a durable local JSONL fallback."""

    def __init__(
        self,
        *,
        client: CoordinatorClient,
        jsonl_path: Path,
        config: EventSinkConfig | None = None,
        sleep: SleepFn = asyncio.sleep,
    ) -> None:
        self._client = client
        self._jsonl_path = jsonl_path
        self._config = config or EventSinkConfig()
        self._sleep = sleep
        self._ingress: queue.SimpleQueue[AgentEvent] = queue.SimpleQueue()
        self._wake = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._running = False

    # ── EventSink contract (hot path) ────────────────────────────────────────

    def emit(self, event: AgentEvent) -> None:
        """Enqueue an event. Non-blocking and safe from any thread."""
        self._ingress.put(event)  # SimpleQueue is unbounded; never blocks
        loop = self._loop
        if loop is None:
            return  # sender not started yet; the backlog drains on start()
        try:
            loop.call_soon_threadsafe(self._wake.set)
        except RuntimeError:
            logger.debug("event loop closed; event buffered for next drain")

    # ── Lifecycle (run on the asyncio loop) ──────────────────────────────────

    def start(self) -> None:
        """Start the background sender. Must be called from the event loop."""
        if self._task is not None and not self._task.done():
            return
        self._loop = asyncio.get_running_loop()
        self._running = True
        self._wake.set()  # drain any events emitted before start()
        self._task = asyncio.create_task(self._run(), name="fallow-event-sink")

    async def stop(self) -> None:
        """Stop the sender and flush any remaining events to the JSONL."""
        self._running = False
        self._wake.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._drain()  # final durability flush

    # ── Sender loop ──────────────────────────────────────────────────────────

    async def _run(self) -> None:
        while self._running:
            await self._wake.wait()
            self._wake.clear()
            await self._drain()
        await self._drain()

    async def _drain(self) -> None:
        while True:
            try:
                event = self._ingress.get_nowait()
            except queue.Empty:
                return
            await self._handle(event)

    async def _handle(self, event: AgentEvent) -> None:
        self._append_jsonl(event)  # durable copy first
        await self._push_best_effort(event)  # then best-effort coordinator push

    def _append_jsonl(self, event: AgentEvent) -> None:
        try:
            self._jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            with self._jsonl_path.open("a", encoding="utf-8") as handle:
                handle.write(event.model_dump_json() + "\n")
        except OSError:
            logger.exception("failed to append event to %s", self._jsonl_path)

    async def _push_best_effort(self, event: AgentEvent) -> None:
        attempts = self._config.max_push_attempts
        for attempt in range(1, attempts + 1):
            try:
                await self._client.push_event(event)
                return
            except CoordinatorAuthError:
                logger.warning("event push unauthorized; dropping (kept in JSONL)")
                return
            except CoordinatorError as exc:
                logger.warning("event push attempt %d/%d failed: %s", attempt, attempts, exc)
                if attempt < attempts:
                    await self._sleep(self._config.backoff_base_s * (2 ** (attempt - 1)))
        logger.warning("dropping event after %d attempt(s) (kept in JSONL)", attempts)
