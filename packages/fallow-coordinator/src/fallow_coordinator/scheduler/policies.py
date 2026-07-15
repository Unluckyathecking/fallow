"""Scheduler v1 policies — the experiment arms (module C4).

Every policy is a pure implementation of
:class:`fallow_protocol.interfaces.SchedulerPolicy`: given identical inputs it
returns identical outputs, so arms are hot-swappable and the experiment stays
reproducible. The only exception is :class:`RoundRobinScheduler`'s cursor,
documented on that class.
"""

from collections.abc import Sequence

from fallow_coordinator.scheduler._eligibility import (
    agent_has_gpu,
    has_warm_replica,
    is_eligible,
)
from fallow_protocol.interfaces import SchedulerPolicy
from fallow_protocol.messages import AgentSnapshot, ReplicaEndpoint


def _endpoint_sort_key(replica: ReplicaEndpoint) -> tuple[int, str]:
    """Least-inflight first, ties broken on ``host:port`` lexicographically."""
    return (replica.inflight, f"{replica.host}:{replica.port}")


def _replicas_for(model_id: str, replicas: Sequence[ReplicaEndpoint]) -> list[ReplicaEndpoint]:
    """Defensively narrow to replicas actually serving ``model_id``."""
    return [replica for replica in replicas if replica.model_id == model_id]


class CapabilityScheduler(SchedulerPolicy):
    """Experiment arm (c): capability-aware placement — the v1 default.

    Stateless and deterministic. ``select_agent`` keeps only eligible agents
    (idle, fresh, GPU-capable when required) and ranks them by, in order:

    1. already holds a warm (READY/SUSPENDED) replica of the model — avoids a
       cold model load;
    2. has any GPU — faster and frees CPU agents for CPU work;
    3. most free RAM;

    with ``agent_id`` as the final, deterministic tiebreak.
    """

    def select_agent(
        self, requirements_model_id: str, needs_gpu: bool, agents: Sequence[AgentSnapshot]
    ) -> str | None:
        eligible = [agent for agent in agents if is_eligible(agent, needs_gpu)]
        if not eligible:
            return None
        best = min(eligible, key=lambda agent: self._rank(requirements_model_id, agent))
        return best.agent_id

    @staticmethod
    def _rank(model_id: str, agent: AgentSnapshot) -> tuple[bool, bool, int, str]:
        # Ascending sort: leading ``False`` / larger negative RAM sort first, so
        # each component is negated to make "better" compare "smaller".
        return (
            not has_warm_replica(agent, model_id),
            not agent_has_gpu(agent),
            -agent.mem_available_mb,
            agent.agent_id,
        )

    def pick_replica(
        self, model_id: str, replicas: Sequence[ReplicaEndpoint]
    ) -> ReplicaEndpoint | None:
        candidates = _replicas_for(model_id, replicas)
        if not candidates:
            return None
        return min(candidates, key=_endpoint_sort_key)


class RoundRobinScheduler(SchedulerPolicy):
    """Experiment arm (b): capability-blind round-robin baseline.

    Round-robin cannot be stateless, so this policy carries exactly one piece of
    state — an integer cursor — and nothing else. By design it ignores every
    capability signal (warm replicas, GPU, free RAM): it applies only the
    correctness eligibility gate, then hands work out in a fair rotation over the
    ``agent_id``-sorted eligible set. A single cursor advances on every handout
    (agents and replicas alike). ``reset()`` restores the deterministic start so
    experiment runs and tests are reproducible.
    """

    def __init__(self) -> None:
        self._cursor = 0

    def reset(self) -> None:
        """Return the cursor to its deterministic starting position (0)."""
        self._cursor = 0

    def _next_index(self, length: int) -> int:
        """Return the current rotation slot and advance the cursor by one."""
        index = self._cursor % length
        self._cursor += 1
        return index

    def select_agent(
        self, requirements_model_id: str, needs_gpu: bool, agents: Sequence[AgentSnapshot]
    ) -> str | None:
        eligible = sorted(
            (agent for agent in agents if is_eligible(agent, needs_gpu)),
            key=lambda agent: agent.agent_id,
        )
        if not eligible:
            return None
        return eligible[self._next_index(len(eligible))].agent_id

    def pick_replica(
        self, model_id: str, replicas: Sequence[ReplicaEndpoint]
    ) -> ReplicaEndpoint | None:
        candidates = sorted(_replicas_for(model_id, replicas), key=_endpoint_sort_key)
        if not candidates:
            return None
        return candidates[self._next_index(len(candidates))]
