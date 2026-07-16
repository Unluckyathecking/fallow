"""Pure fit check: model minimums vs an agent's reported capacity."""

from __future__ import annotations

from scheduler_helpers import make_caps

from fallow_coordinator.scheduler import model_fit
from fallow_protocol.capabilities import GpuStatus, WorkerKind
from fallow_protocol.messages import AgentSnapshot, AgentState
from fallow_protocol.models import ModelManifest


def _manifest(*, min_ram_mb: int = 0, min_vram_mb: int = 0) -> ModelManifest:
    return ModelManifest(
        model_id="m1",
        family="m",
        quant="Q4_K_M",
        worker_kind=WorkerKind.CHAT,
        file_name="m1.gguf",
        sha256="0" * 64,
        size_bytes=1024,
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


def test_cpu_model_fits_when_ram_is_enough() -> None:
    report = model_fit(_manifest(min_ram_mb=4096), _agent(mem_available_mb=8192))
    assert report.fits
    assert report.required_ram_mb == 4096
    assert report.available_ram_mb == 8192
    assert report.required_vram_mb == 0
    assert report.available_vram_mb == 0


def test_ram_requirement_over_available_does_not_fit() -> None:
    report = model_fit(_manifest(min_ram_mb=100_000), _agent(mem_available_mb=8192))
    assert not report.fits
    assert report.required_ram_mb == 100_000
    assert report.available_ram_mb == 8192


def test_vram_requirement_without_gpu_does_not_fit() -> None:
    report = model_fit(_manifest(min_vram_mb=8000), _agent(gpus=()))
    assert not report.fits
    assert report.required_vram_mb == 8000
    assert report.available_vram_mb == 0


def test_vram_fits_on_the_roomiest_gpu() -> None:
    gpus = (
        GpuStatus(index=0, vram_free_mb=4000, util_percent=0.0),
        GpuStatus(index=1, vram_free_mb=12000, util_percent=0.0),
    )
    report = model_fit(_manifest(min_vram_mb=10000), _agent(gpus=gpus))
    assert report.fits
    assert report.available_vram_mb == 12000


def test_vram_over_every_gpu_does_not_fit() -> None:
    gpus = (GpuStatus(index=0, vram_free_mb=4000, util_percent=0.0),)
    report = model_fit(_manifest(min_vram_mb=10000), _agent(gpus=gpus))
    assert not report.fits
    assert report.available_vram_mb == 4000
