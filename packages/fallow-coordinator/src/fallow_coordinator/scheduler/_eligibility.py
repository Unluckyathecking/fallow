"""Pure eligibility predicates shared by the scheduler policies.

No I/O, no clocks, no randomness: every function is a deterministic view over an
:class:`AgentSnapshot`. Both experiment arms share the *eligibility* gate (an
ineligible agent literally cannot serve a unit); they differ only in how they
*rank* the eligible set.
"""

from fallow_protocol.messages import AgentSnapshot, AgentState
from fallow_protocol.models import ReplicaState

# A replica already loaded for a model — whether serving or merely suspended —
# lets an agent take that model's work without a cold model load.
_WARM_REPLICA_STATES: frozenset[ReplicaState] = frozenset(
    {ReplicaState.READY, ReplicaState.SUSPENDED}
)


def agent_has_gpu(agent: AgentSnapshot) -> bool:
    """True when the machine registered at least one GPU."""
    return len(agent.caps.gpus) > 0


def agent_is_fit(agent: AgentSnapshot) -> bool:
    """True when the agent may run *any* fallow work right now.

    Fitness is capability-agnostic: the user must be away (``IDLE``) and the
    coordinator's view of the agent must be fresh (not ``suspect``).
    """
    return agent.state == AgentState.IDLE and not agent.suspect


def is_eligible(agent: AgentSnapshot, needs_gpu: bool) -> bool:
    """True when ``agent`` can serve a unit with the given GPU requirement."""
    return agent_is_fit(agent) and (not needs_gpu or agent_has_gpu(agent))


def has_warm_replica(agent: AgentSnapshot, model_id: str) -> bool:
    """True when the agent already holds a READY or SUSPENDED replica of the model."""
    return any(
        replica.model_id == model_id and replica.state in _WARM_REPLICA_STATES
        for replica in agent.replicas
    )
