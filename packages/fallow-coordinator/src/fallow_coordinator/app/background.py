"""Background maintenance loops for the coordinator app (module I1).

Two periodic loops run for the app's lifetime, both driven by the injected clock
and sleeper so they are deterministic and cancellable:

* the scheduler's :class:`DispatchLoop` (``requeue_expired`` sweep), and
* the offline-eviction loop, which requeues every unit still leased to an agent
  the registry now considers offline.

Both share ``config.requeue_interval_s`` cadence and neither may ever die on a
store error — a bad iteration is swallowed so the loop keeps running.
"""

from __future__ import annotations

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
