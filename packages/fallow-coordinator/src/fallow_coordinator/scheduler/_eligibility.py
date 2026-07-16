"""Pure eligibility predicates shared by the scheduler policies.

No I/O, no clocks, no randomness: every function is a deterministic view over an
:class:`AgentSnapshot`. Both experiment arms share the *eligibility* gate (an
ineligible agent literally cannot serve a unit); they differ only in how they
*rank* the eligible set.
"""

from typing import NamedTuple

from fallow_protocol.messages import AgentSnapshot, AgentState
from fallow_protocol.models import ModelManifest, ReplicaState

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


class FitReport(NamedTuple):
    """A model's declared minimums against an agent's reported capacity."""

    fits: bool
    required_vram_mb: int
    required_ram_mb: int
    available_vram_mb: int
    available_ram_mb: int


def agent_available_vram_mb(agent: AgentSnapshot) -> int:
    """Largest single-GPU free VRAM the agent reports (0 when it has no GPU).

    A replica loads onto one GPU, so the fit is against the biggest device, not
    the sum across devices.
    """
    return max((gpu.vram_free_mb for gpu in agent.gpus), default=0)


def model_fit(model: ModelManifest, agent: AgentSnapshot) -> FitReport:
    """Whether ``agent`` can hold ``model`` given its latest reported capacity.

    Capacity is the live view: available RAM from the heartbeat and free VRAM on
    the roomiest GPU. Requirements are the manifest's declared minimums.
    """
    available_vram_mb = agent_available_vram_mb(agent)
    available_ram_mb = agent.mem_available_mb
    fits = model.min_ram_mb <= available_ram_mb and model.min_vram_mb <= available_vram_mb
    return FitReport(
        fits=fits,
        required_vram_mb=model.min_vram_mb,
        required_ram_mb=model.min_ram_mb,
        available_vram_mb=available_vram_mb,
        available_ram_mb=available_ram_mb,
    )
