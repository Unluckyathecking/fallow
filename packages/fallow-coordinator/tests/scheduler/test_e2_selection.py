"""Largest-fitting-model selection (ADR 048): pure policy over fabricated agents."""

from __future__ import annotations

from scheduler_helpers import make_caps

from fallow_coordinator.scheduler import capacity_snapshot, select_model_for_agent
from fallow_protocol.capabilities import GpuStatus, WorkerKind
from fallow_protocol.messages import AgentSnapshot, AgentState
from fallow_protocol.models import ModelManifest


def _manifest(
    model_id: str, *, size_bytes: int = 1024, min_ram_mb: int = 0, min_vram_mb: int = 0
) -> ModelManifest:
    return ModelManifest(
        model_id=model_id,
        family="m",
        quant="Q4_K_M",
        worker_kind=WorkerKind.CHAT,
        file_name=f"{model_id}.gguf",
        sha256="0" * 64,
        size_bytes=size_bytes,
        min_ram_mb=min_ram_mb,
        min_vram_mb=min_vram_mb,
    )


def _agent(*, mem_available_mb: int = 8192, gpus: tuple[GpuStatus, ...] = ()) -> AgentSnapshot:
    return AgentSnapshot(
        agent_id="a1",
        host="10.0.0.1",
        state=AgentState.IDLE,
        suspect=False,
        caps=make_caps(gpu_count=len(gpus)),
        mem_available_mb=mem_available_mb,
        gpus=gpus,
    )


def test_picks_the_largest_model_that_fits() -> None:
    big = _manifest("big", size_bytes=8000, min_ram_mb=4096)
    small = _manifest("small", size_bytes=1000, min_ram_mb=1024)
    chosen = select_model_for_agent(_agent(mem_available_mb=8192), [small, big])
    assert chosen is not None
    assert chosen.model_id == "big"


def test_skips_models_that_do_not_fit_and_takes_the_largest_remaining() -> None:
    too_big = _manifest("too-big", size_bytes=9000, min_ram_mb=100_000)
    fits = _manifest("fits", size_bytes=2000, min_ram_mb=4096)
    chosen = select_model_for_agent(_agent(mem_available_mb=8192), [too_big, fits])
    assert chosen is not None
    assert chosen.model_id == "fits"


def test_returns_none_when_nothing_fits() -> None:
    gpu_only = _manifest("gpu-only", min_vram_mb=8000)
    huge = _manifest("huge", min_ram_mb=100_000)
    assert select_model_for_agent(_agent(mem_available_mb=8192), [gpu_only, huge]) is None


def test_returns_none_for_an_empty_registry() -> None:
    assert select_model_for_agent(_agent(), []) is None


def test_gpu_agent_prefers_a_gpu_model_over_a_larger_cpu_model() -> None:
    gpus = (GpuStatus(index=0, vram_free_mb=12000, util_percent=0.0),)
    gpu_model = _manifest("gpu", size_bytes=2000, min_vram_mb=8000)
    bigger_cpu_model = _manifest("cpu", size_bytes=9000, min_ram_mb=4096)
    chosen = select_model_for_agent(_agent(gpus=gpus), [bigger_cpu_model, gpu_model])
    assert chosen is not None
    assert chosen.model_id == "gpu"


def test_tie_breaks_deterministically_on_model_id() -> None:
    first = _manifest("aaa", size_bytes=4096, min_ram_mb=1024)
    second = _manifest("bbb", size_bytes=4096, min_ram_mb=1024)
    # Same size and fit: order of the input list must not change the winner.
    assert select_model_for_agent(_agent(), [first, second]).model_id == "aaa"
    assert select_model_for_agent(_agent(), [second, first]).model_id == "aaa"


def test_capacity_snapshot_exposes_total_ram_and_per_gpu_vram() -> None:
    caps = make_caps(gpu_count=1)  # 16384 MB RAM, one 8192 MB GPU
    snapshot = capacity_snapshot("agent-x", caps)
    fits_on_ram = _manifest("ram", min_ram_mb=16384)
    fits_on_vram = _manifest("vram", min_vram_mb=8192)
    over_vram = _manifest("over", min_vram_mb=8193)
    assert select_model_for_agent(snapshot, [fits_on_ram]).model_id == "ram"
    assert select_model_for_agent(snapshot, [fits_on_vram]).model_id == "vram"
    assert select_model_for_agent(snapshot, [over_vram]) is None
