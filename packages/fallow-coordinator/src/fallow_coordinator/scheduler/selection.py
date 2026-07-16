"""Largest-fitting-model selection for auto-assignment on enroll (ADR 048).

Pure functions over an :class:`AgentSnapshot`: no I/O, no clocks. The fit gate is
the one shipped for capability-aware assignment (``_eligibility.model_fit``), so
enroll-time placement and the operator ``flw assign`` path agree on "can this
agent hold this model". This module only adds the *policy* on top of that gate:
pick the single largest model that fits.
"""

from collections.abc import Sequence

from fallow_coordinator.scheduler._eligibility import agent_has_gpu, model_fit
from fallow_protocol.capabilities import DeviceCaps, GpuStatus
from fallow_protocol.messages import AgentSnapshot, AgentState
from fallow_protocol.models import ModelManifest


def capacity_snapshot(agent_id: str, caps: DeviceCaps) -> AgentSnapshot:
    """A view of the machine at its full declared capacity, for enroll-time fit.

    A just-enrolled agent has sent no heartbeat, so its live free RAM/VRAM read
    as zero. Fit at enroll is instead against what the machine *is*: total RAM
    and each GPU's total VRAM from the registration caps. Feeding that through
    the same ``model_fit`` keeps one fit definition for enroll and ``flw assign``.
    """
    gpus = tuple(
        GpuStatus(index=gpu.index, vram_free_mb=gpu.vram_mb, util_percent=0.0) for gpu in caps.gpus
    )
    return AgentSnapshot(
        agent_id=agent_id,
        host="",
        state=AgentState.IDLE,
        suspect=False,
        caps=caps,
        mem_available_mb=caps.ram_mb,
        gpus=gpus,
    )


def select_model_for_agent(
    agent: AgentSnapshot, models: Sequence[ModelManifest]
) -> ModelManifest | None:
    """Pick the single best-fitting model for ``agent``, or None if none fit.

    "Best" is the largest model that fits, by ``size_bytes``. A GPU-capable agent
    prefers a model that actually uses the GPU (``min_vram_mb > 0``) over a
    CPU-only one, so idle VRAM does real work. Ties break on ``model_id`` so the
    choice is deterministic and stable across restarts.
    """
    fitting = [model for model in models if model_fit(model, agent).fits]
    if not fitting:
        return None
    prefer_gpu = agent_has_gpu(agent)
    return min(fitting, key=lambda model: _rank(model, prefer_gpu))


def _rank(model: ModelManifest, prefer_gpu: bool) -> tuple[bool, int, str]:
    """Sort key (ascending ``min`` wins): GPU model first on a GPU agent, then
    larger by size, then lexically smallest ``model_id``."""
    uses_gpu = model.min_vram_mb > 0
    return (not (prefer_gpu and uses_gpu), -model.size_bytes, model.model_id)
