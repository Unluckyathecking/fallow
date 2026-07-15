"""``AgentServices``: start/stop lifecycle for the assembled agent.

This is the orchestration seam: it owns the started components and the exact
startup and (graceful) shutdown *order*. The assembly builds it; the runtime
drives it. Keeping the ordering here — separate from the wiring in ``assembly``
— lets it be unit-tested with recording fakes, which is how the drain-before-
stop_all guarantee is pinned.

Shutdown order (ADR 015): drain the preemptor (stop accepting, emit
AGENT_STOPPING), stop the work + reconcile loops, stop the periodic heartbeat,
send one final DRAINING heartbeat, stop the poll thread, stop every replica,
then flush the event sink to its durable JSONL.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from fallow_agent.main.protocols import PreemptorLike, SupervisorLike

logger = logging.getLogger(__name__)

_POLL_STOP_TIMEOUT_S = 2.0

FinalHeartbeat = Callable[[], Awaitable[None]]


class AsyncLoopLike(Protocol):
    """An asyncio loop component (reconcile / work)."""

    def start(self) -> None: ...

    def request_stop(self) -> None: ...

    async def stop(self) -> None: ...


class HeartbeatLike(Protocol):
    """The periodic heartbeat beacon."""

    def start(self) -> None: ...

    async def stop(self) -> None: ...


class PollLoopLike(Protocol):
    """The synchronous preemption poll thread."""

    def start(self) -> None: ...

    def stop(self, timeout: float | None = ...) -> None: ...


class SinkLike(Protocol):
    """The durable event sink."""

    def start(self) -> None: ...

    async def stop(self) -> None: ...


class AgentServices:
    """Owns the started components and their startup/shutdown ordering."""

    def __init__(
        self,
        *,
        preemptor: PreemptorLike,
        supervisor: SupervisorLike,
        event_sink: SinkLike,
        poll_loop: PollLoopLike,
        heartbeat: HeartbeatLike,
        reconcile_loop: AsyncLoopLike,
        work_loop: AsyncLoopLike,
        final_heartbeat: FinalHeartbeat,
    ) -> None:
        self._preemptor = preemptor
        self._supervisor = supervisor
        self._event_sink = event_sink
        self._poll_loop = poll_loop
        self._heartbeat = heartbeat
        self._reconcile_loop = reconcile_loop
        self._work_loop = work_loop
        self._final_heartbeat = final_heartbeat

    def start(self) -> None:
        """Start every component (event sink first, then loops)."""
        self._event_sink.start()
        self._poll_loop.start()
        self._heartbeat.start()
        self._reconcile_loop.start()
        self._work_loop.start()

    async def stop(self) -> None:
        """Gracefully stop everything in the ADR-015 order."""
        self._preemptor.drain()
        self._work_loop.request_stop()
        self._reconcile_loop.request_stop()
        await self._work_loop.stop()
        await self._reconcile_loop.stop()
        await self._heartbeat.stop()
        await self._send_final_heartbeat()
        self._poll_loop.stop(_POLL_STOP_TIMEOUT_S)
        self._supervisor.stop_all()
        await self._event_sink.stop()

    async def _send_final_heartbeat(self) -> None:
        try:
            await self._final_heartbeat()
        except Exception:
            logger.warning("final heartbeat attempt failed; shutting down anyway")
