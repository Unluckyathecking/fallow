"""Churn-aware scheduler v2 — experiment arm (c) v2 (scheduler module C6).

Where :class:`CapabilityScheduler` (v1) ranks eligible agents by static capability
(warm replica, GPU, free RAM), :class:`ChurnAwareScheduler` adds the missing
*temporal* signal: an agent that is statistically about to have its user return is
a poor place to park a batch unit, because the preemptor will yield it mid-flight
and the unit gets requeued. It ranks eligible agents by the modelled probability
that they stay idle long enough to finish the unit, then falls back to the exact
v1 capability order to break ties deterministically.

Only *batch placement* (``select_agent``) is churn-ranked. ``pick_replica`` — the
interactive path — stays identical to v1 (least-inflight): interactive requests
are short, a mid-stream yield truncates at most one response (ADR 000), and there
is no requeue to avoid, so churn ranking buys nothing there. See ADR 022.

Purity: the policy is a pure function of ``(model, hour_fn)``. The only
non-argument input, the current hour-of-day, is read through the injected
``hour_fn`` so the arm stays replay-deterministic.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from fallow_coordinator.scheduler._eligibility import (
    agent_has_gpu,
    has_warm_replica,
    is_eligible,
)
from fallow_coordinator.scheduler.churn_model import ChurnModel
from fallow_coordinator.scheduler.policies import _endpoint_sort_key, _replicas_for
from fallow_protocol.interfaces import SchedulerPolicy
from fallow_protocol.messages import AgentSnapshot, ReplicaEndpoint

# Default assumed batch-unit duration (seconds) the survival horizon is measured
# against; overridden per-deployment by ``CoordinatorConfig.churn_est_unit_duration_s``.
DEFAULT_EST_UNIT_DURATION_S = 60.0

# Returns the current hour-of-day (0-23); injected so the policy reads no clock.
HourFn = Callable[[], int]

# Ranking key: (-P(stays idle), then the v1 capability order) — see ``_rank``.
_Rank = tuple[float, bool, bool, int, str]


class ChurnAwareScheduler(SchedulerPolicy):
    """Experiment arm (c) v2: churn-aware batch placement, capability tiebreak.

    ``select_agent`` filters to eligible agents exactly as v1 does (``IDLE``, not
    ``suspect``, GPU-capable when required), then ranks them by, in order:

    1. **highest** modelled ``P(stays idle >= est_unit_duration_s)`` given the
       agent's current idle age — the churn signal;
    2-4. the v1 capability order as a deterministic tiebreak: warm replica of the
       model, then any GPU, then most free RAM;
    5. ``agent_id`` as the final tiebreak.
    """

    def __init__(
        self,
        model: ChurnModel,
        est_unit_duration_s: float = DEFAULT_EST_UNIT_DURATION_S,
        *,
        hour_fn: HourFn,
    ) -> None:
        self._model = model
        self._est_unit_duration_s = est_unit_duration_s
        self._hour_fn = hour_fn

    def select_agent(
        self, requirements_model_id: str, needs_gpu: bool, agents: Sequence[AgentSnapshot]
    ) -> str | None:
        eligible = [agent for agent in agents if is_eligible(agent, needs_gpu)]
        if not eligible:
            return None
        hour = self._hour_fn()
        best = min(eligible, key=lambda agent: self._rank(requirements_model_id, hour, agent))
        return best.agent_id

    def _rank(self, model_id: str, hour: int, agent: AgentSnapshot) -> _Rank:
        # ``agent.user_idle_s`` is the current idle age u; condition survival on it
        # so a machine already idle a long time is credited its accumulated tenure.
        stays_idle = self._model.survival(
            agent.agent_id, hour, agent.user_idle_s, self._est_unit_duration_s
        )
        # Ascending sort: negate the probability so "more likely to stay" sorts
        # first, then reuse the v1 negated-capability order for the tiebreak.
        return (
            -stays_idle,
            not has_warm_replica(agent, model_id),
            not agent_has_gpu(agent),
            -agent.mem_available_mb,
            agent.agent_id,
        )

    def pick_replica(
        self, model_id: str, replicas: Sequence[ReplicaEndpoint]
    ) -> ReplicaEndpoint | None:
        # Identical to v1: interactive routing is least-inflight; churn ranking is a
        # batch-placement concern only (ADR 022).
        candidates = _replicas_for(model_id, replicas)
        if not candidates:
            return None
        return min(candidates, key=_endpoint_sort_key)
