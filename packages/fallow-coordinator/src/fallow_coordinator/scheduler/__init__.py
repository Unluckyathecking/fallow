"""Scheduler v1 policies and dispatch orchestrator (coordinator module C4).

Public API:

- :class:`CapabilityScheduler` — capability-aware placement, the v1 default and
  experiment arm (c).
- :class:`RoundRobinScheduler` — capability-blind round-robin, experiment arm (b).
- :class:`DispatchLoop` / :class:`DispatchStats` — the impure loop that sweeps
  expired leases and hosts the policy for the app layer.
- :func:`select_for_poll` — per-long-poll model-id pre-filter for
  ``queue.lease_next``.

Policies are pure implementations of
:class:`fallow_protocol.interfaces.SchedulerPolicy`; the loop is the only impure
piece and depends on the injected clock, queue store, and snapshot source.
"""

from fallow_coordinator.scheduler.dispatch import DispatchLoop, DispatchStats
from fallow_coordinator.scheduler.policies import (
    CapabilityScheduler,
    RoundRobinScheduler,
)
from fallow_coordinator.scheduler.poll import select_for_poll

__all__ = [
    "CapabilityScheduler",
    "DispatchLoop",
    "DispatchStats",
    "RoundRobinScheduler",
    "select_for_poll",
]
