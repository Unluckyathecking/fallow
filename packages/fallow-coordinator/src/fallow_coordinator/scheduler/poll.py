"""Long-poll pre-filter helper (module C4).

Work handout in v0.1 is PULL-based: an idle agent long-polls and the app layer
calls :func:`queue.lease_next` with a list of model_ids the agent is allowed to
run. :func:`select_for_poll` computes that pre-filtered list so an unfit agent
never leases work.
"""

from collections.abc import Sequence

from fallow_coordinator.scheduler._eligibility import agent_is_fit
from fallow_protocol.interfaces import SchedulerPolicy
from fallow_protocol.messages import AgentSnapshot


def select_for_poll(
    agent: AgentSnapshot,
    model_ids: Sequence[str],
    policy: SchedulerPolicy,
) -> tuple[str, ...]:
    """Return the subset of ``model_ids`` this agent may lease right now.

    Empty when the agent is unfit (not IDLE, or heartbeat-suspect); otherwise the
    caller feeds the result straight to ``queue.lease_next``.

    ``policy`` is part of the stable signature the app calls on every long-poll.
    v1 fitness is capability-agnostic, so every arm admits any model a fit agent
    is assigned; the parameter is threaded now so the v2 (push-aware) policy can
    prune per-model without changing this call site or the app layer.
    """
    if not agent_is_fit(agent):
        return ()
    return tuple(model_ids)
