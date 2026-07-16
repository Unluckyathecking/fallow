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

Idle-survival alone credits a machine that stays idle but never *finishes* a
unit. So placement also folds in a task-success reliability signal
(:class:`ReliabilityModel`, ADR 055) as a bounded secondary weight: the primary
score is ``survival + reliability_weight * reliability``. Because reliability is
in ``[0, 1]`` and the weight is small, an agent trailing on survival by the
weight or more can never be promoted by reliability — survival stays primary and
reliability only shapes genuine near-ties. See ADR 055.

Purity: the policy is a pure function of ``(churn model, reliability model,
hour_fn)``. The only non-argument input, the current hour-of-day, is read through
the injected ``hour_fn`` so the arm stays replay-deterministic.
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
from fallow_coordinator.scheduler.reliability import ReliabilityModel
from fallow_protocol.interfaces import SchedulerPolicy
from fallow_protocol.messages import AgentSnapshot, ReplicaEndpoint

# Default assumed batch-unit duration (seconds) the survival horizon is measured
# against; overridden per-deployment by ``CoordinatorConfig.churn_est_unit_duration_s``.
DEFAULT_EST_UNIT_DURATION_S = 60.0

# How much a perfectly-reliable agent adds to its survival-based score. Bounded
# and small so idle-survival stays the primary signal: a survival gap of at least
# this much is never overturned by reliability (both scores are in [0, 1]).
DEFAULT_RELIABILITY_WEIGHT = 0.1

# Returns the current hour-of-day (0-23); injected so the policy reads no clock.
HourFn = Callable[[], int]

# Ranking key: (-blended placement score, then the v1 capability order) — see ``_rank``.
_Rank = tuple[float, bool, bool, int, str]


class ChurnAwareScheduler(SchedulerPolicy):
    """Experiment arm (c) v2: churn-aware batch placement, capability tiebreak.

    ``select_agent`` filters to eligible agents exactly as v1 does (``IDLE``, not
    ``suspect``, GPU-capable when required), then ranks them by, in order:

    1. **highest** placement score ``survival + reliability_weight * reliability``,
       where ``survival`` is the modelled
       ``P(stays idle >= est_unit_duration_s)`` given the agent's current idle
       age (the primary churn signal) and ``reliability`` is the agent's
       task-success rate (a bounded secondary weight; see module docstring);
    2-4. the v1 capability order as a deterministic tiebreak: warm replica of the
       model, then any GPU, then most free RAM;
    5. ``agent_id`` as the final tiebreak.

    ``reliability`` defaults to ``None``, in which case the score is exactly the
    idle-survival probability and behaviour is identical to the churn-only arm.
    """

    def __init__(
        self,
        model: ChurnModel,
        est_unit_duration_s: float = DEFAULT_EST_UNIT_DURATION_S,
        *,
        hour_fn: HourFn,
        reliability: ReliabilityModel | None = None,
        reliability_weight: float = DEFAULT_RELIABILITY_WEIGHT,
    ) -> None:
        self._model = model
        self._est_unit_duration_s = est_unit_duration_s
        self._hour_fn = hour_fn
        self._reliability = reliability
        self._reliability_weight = reliability_weight

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
        # Fold in task-success reliability as a bounded secondary weight, so that
        # among agents with comparable survival the one that finishes work wins.
        score = stays_idle + self._reliability_weight * self._reliability_of(agent.agent_id)
        # Ascending sort: negate the score so "better placement" sorts first, then
        # reuse the v1 negated-capability order for the tiebreak.
        return (
            -score,
            not has_warm_replica(agent, model_id),
            not agent_has_gpu(agent),
            -agent.mem_available_mb,
            agent.agent_id,
        )

    def _reliability_of(self, agent_id: str) -> float:
        """The agent's task-success rate, or 0.0 when no reliability model is set.

        A missing model contributes nothing to the score, so the arm degrades to
        pure idle-survival ranking (identical to the churn-only behaviour).
        """
        if self._reliability is None:
            return 0.0
        return self._reliability.success_rate(agent_id)

    def pick_replica(
        self, model_id: str, replicas: Sequence[ReplicaEndpoint]
    ) -> ReplicaEndpoint | None:
        # Identical to v1: interactive routing is least-inflight; churn ranking is a
        # batch-placement concern only (ADR 022).
        candidates = _replicas_for(model_id, replicas)
        if not candidates:
            return None
        return min(candidates, key=_endpoint_sort_key)
