"""Scheduler v1 policies and dispatch orchestrator (coordinator module C4).

Public API:

- :class:`CapabilityScheduler` — capability-aware placement, the v1 default and
  experiment arm (c).
- :class:`RoundRobinScheduler` — capability-blind round-robin, experiment arm (b).
- :class:`ChurnAwareScheduler` — churn-aware placement, experiment arm (c) v2.
- :class:`ChurnModel` / :func:`build_churn_model` — the empirical idle-survival
  model the v2 arm ranks by, built (pure) from ``events.jsonl`` mappings.
- :class:`DispatchLoop` / :class:`DispatchStats` — the impure loop that sweeps
  expired leases and hosts the policy for the app layer.
- :func:`select_for_poll` — per-long-poll model-id pre-filter for
  ``queue.lease_next``.

Policies are pure implementations of
:class:`fallow_protocol.interfaces.SchedulerPolicy`; the loop is the only impure
piece and depends on the injected clock, queue store, and snapshot source.
"""

from fallow_coordinator.scheduler._eligibility import FitReport, model_fit
from fallow_coordinator.scheduler.churn_model import ChurnModel, build_churn_model
from fallow_coordinator.scheduler.dispatch import DispatchLoop, DispatchStats
from fallow_coordinator.scheduler.policies import (
    CapabilityScheduler,
    RoundRobinScheduler,
)
from fallow_coordinator.scheduler.poll import select_for_poll
from fallow_coordinator.scheduler.v2 import ChurnAwareScheduler

__all__ = [
    "CapabilityScheduler",
    "ChurnAwareScheduler",
    "ChurnModel",
    "DispatchLoop",
    "DispatchStats",
    "FitReport",
    "RoundRobinScheduler",
    "build_churn_model",
    "model_fit",
    "select_for_poll",
]
