"""Background maintenance loops for the coordinator app (module I1).

Three periodic loops run for the app's lifetime. They use the injected clock and
sleeper so tests can control their cadence:

* the scheduler's :class:`DispatchLoop` (``requeue_expired`` sweep), and
* the offline-eviction loop, which requeues every unit still leased to an agent
  the registry now considers offline, and
* the gateway quota snapshot loop, which writes in-memory counters to the registry.

Dispatch and eviction share ``config.requeue_interval_s``. Quota snapshots use
``config.quota_snapshot_interval_s``. A bad iteration does not stop either maintenance
loop.
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Sequence

from fallow_coordinator.app.state import CoordinatorState
from fallow_protocol.messages import AgentSnapshot


def snapshot_source(state: CoordinatorState) -> Awaitable[Sequence[AgentSnapshot]]:
    """Return the awaitable current-snapshots view for the dispatch loop."""
    return state.registry.snapshots(state.now())


async def offline_eviction_loop(state: CoordinatorState) -> None:
    """Requeue work leased to offline agents, every ``requeue_interval_s``."""
    while not state.stop_event.is_set():
        try:
            offline = await state.registry.list_offline(state.now())
            for agent_id in offline:
                await state.queue.requeue_agent(agent_id)
        except Exception:  # a bad sweep must never kill the eviction loop
            pass
        await state.sleep(state.config.requeue_interval_s)


async def quota_snapshot_loop(state: CoordinatorState) -> None:
    """Persist gateway quota counters at the configured fixed interval."""
    while not state.stop_event.is_set():
        await state.sleep(state.config.quota_snapshot_interval_s)
        if state.stop_event.is_set():
            return
        with contextlib.suppress(Exception):
            await state.quotas.snapshot()
