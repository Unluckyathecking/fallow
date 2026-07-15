"""The impure dispatch orchestrator that marries policy to stores (module C4).

Policies are pure; this loop is where the clock, the queue store, and the live
agent view meet. In v0.1 a tick does exactly one durable thing — sweep expired
leases — because unit handout is PULL-based (agents long-poll and the app calls
``queue.lease_next``; see :func:`fallow_coordinator.scheduler.select_for_poll`).
The ``policy`` and ``snapshots`` dependencies are held for the app layer's
per-poll ``select_agent`` competition and for the future v2 push dispatch.
"""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime

from fallow_protocol.base import FallowModel
from fallow_protocol.interfaces import QueueStore, SchedulerPolicy
from fallow_protocol.messages import AgentSnapshot

SnapshotSource = Callable[[], Awaitable[Sequence[AgentSnapshot]]]
Sleeper = Callable[[float], Awaitable[None]]


class DispatchStats(FallowModel):
    """Outcome of one :meth:`DispatchLoop.tick`.

    ``error`` is populated (and ``requeued`` is 0) when the tick swallowed a
    store exception instead of letting it kill the loop.
    """

    at: datetime
    requeued: int = 0
    error: str | None = None


class DispatchLoop:
    """Drives periodic queue maintenance without ever dying on store errors."""

    def __init__(
        self,
        queue: QueueStore,
        snapshots: SnapshotSource,
        policy: SchedulerPolicy,
        requeue_interval_s: float,
        now: Callable[[], datetime],
        sleep: Sleeper = asyncio.sleep,
    ) -> None:
        if requeue_interval_s <= 0:
            raise ValueError("requeue_interval_s must be > 0")
        self._queue = queue
        self.snapshots = snapshots
        self.policy = policy
        self._requeue_interval_s = requeue_interval_s
        self._now = now
        self._sleep = sleep
        self._running = False

    async def tick(self) -> DispatchStats:
        """Run one maintenance tick: sweep expired leases, never raising."""
        at = self._now()
        try:
            requeued = await self._queue.requeue_expired()
        except Exception as exc:  # a bad sweep must not kill the loop
            return DispatchStats(at=at, error=repr(exc))
        return DispatchStats(at=at, requeued=requeued)

    async def run_forever(self) -> None:
        """Tick every ``requeue_interval_s`` until :meth:`stop` is called.

        Errors inside a tick are already absorbed by :meth:`tick`, so the loop
        only exits on an explicit stop or a cancellation of the injected sleep.
        """
        self._running = True
        while self._running:
            await self.tick()
            await self._sleep(self._requeue_interval_s)

    def stop(self) -> None:
        """Ask :meth:`run_forever` to exit after the current iteration."""
        self._running = False
