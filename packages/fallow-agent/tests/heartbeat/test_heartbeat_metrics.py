"""Metric collection tests: probe seams, clamping, and the NVML degradation path."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from fallow_agent.heartbeat import (
    NvmlGpuProbe,
    collect_caps,
    collect_heartbeat_metrics,
)
from fallow_agent.heartbeat import probes as probes_module
from fallow_protocol.capabilities import GpuInfo, GpuStatus, OsFamily


class StubSystemProbe:
    """Fully in-memory SystemProbe for deterministic assertions."""

    def __init__(
        self, *, cpu_percent: float = 10.0, cpu_cores: int = 4, cpu_model: str = "x"
    ) -> None:
        self._cpu_percent = cpu_percent
        self._cpu_cores = cpu_cores
        self._cpu_model = cpu_model

    def hostname(self) -> str:
        return "host-x"

    def os_family(self) -> OsFamily:
        return OsFamily.LINUX

    def os_version(self) -> str:
        return "1.0"

    def cpu_model(self) -> str:
        return self._cpu_model

    def cpu_cores(self) -> int:
        return self._cpu_cores

    def ram_mb(self) -> int:
        return 8000

    def disk_free_mb(self) -> int:
        return 50000

    def cpu_percent(self) -> float:
        return self._cpu_percent

    def mem_available_mb(self) -> int:
        return 4000

    def load_avg(self) -> float | None:
        return 1.5

    def temp_cpu_c(self) -> float | None:
        return 40.0


class StubGpuProbe:
    def static_gpus(self) -> tuple[GpuInfo, ...]:
        return (GpuInfo(index=0, name="G", vendor="nvidia", vram_mb=8000),)

    def gpu_statuses(self) -> tuple[GpuStatus, ...]:
        return (GpuStatus(index=0, vram_free_mb=7000, util_percent=5.0),)


def test_collect_caps_maps_probe_values() -> None:
    caps = collect_caps("0.1.0", system=StubSystemProbe(), gpu=StubGpuProbe())

    assert caps.hostname == "host-x"
    assert caps.agent_version == "0.1.0"
    assert caps.cpu_cores == 4
    assert caps.gpus[0].name == "G"


def test_collect_caps_clamps_zero_cpu_cores_and_unknown_model() -> None:
    caps = collect_caps(
        "0.1.0", system=StubSystemProbe(cpu_cores=0, cpu_model=""), gpu=StubGpuProbe()
    )

    assert caps.cpu_cores == 1  # DeviceCaps requires > 0
    assert caps.cpu_model == "unknown"


def test_collect_heartbeat_metrics_returns_ordered_tuple() -> None:
    metrics = collect_heartbeat_metrics(system=StubSystemProbe(), gpu=StubGpuProbe())

    cpu_percent, mem_available_mb, load_avg, gpus, temp_cpu_c = metrics
    assert cpu_percent == 10.0
    assert mem_available_mb == 4000
    assert load_avg == 1.5
    assert gpus[0].util_percent == 5.0
    assert temp_cpu_c == 40.0


@pytest.mark.parametrize(("raw", "expected"), [(-5.0, 0.0), (150.0, 100.0), (50.0, 50.0)])
def test_cpu_percent_is_clamped(raw: float, expected: float) -> None:
    metrics = collect_heartbeat_metrics(system=StubSystemProbe(cpu_percent=raw), gpu=StubGpuProbe())
    assert metrics.cpu_percent == expected


def test_nvml_probe_returns_empty_when_nvml_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probes_module, "_load_nvml", lambda: None)
    probe = NvmlGpuProbe()

    assert probe.static_gpus() == ()
    assert probe.gpu_statuses() == ()


def _fake_nvml(*, raise_on_read: bool = False) -> SimpleNamespace:
    def get_count() -> int:
        return 1

    def get_handle(index: int) -> str:
        if raise_on_read:
            raise RuntimeError("nvml read failed")
        return f"h{index}"

    return SimpleNamespace(
        NVML_TEMPERATURE_GPU=0,
        nvmlInit=lambda: None,
        nvmlShutdown=lambda: None,
        nvmlDeviceGetCount=get_count,
        nvmlDeviceGetHandleByIndex=get_handle,
        nvmlDeviceGetMemoryInfo=lambda h: SimpleNamespace(
            total=8 * 1024 * 1024 * 1024, free=6 * 1024 * 1024 * 1024
        ),
        nvmlDeviceGetName=lambda h: b"NVIDIA RTX",
        nvmlDeviceGetUtilizationRates=lambda h: SimpleNamespace(gpu=42),
        nvmlDeviceGetPowerUsage=lambda h: 120000,  # milliwatts
        nvmlDeviceGetTemperature=lambda h, s: 55,
    )


def test_nvml_probe_parses_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probes_module, "_load_nvml", _fake_nvml)
    probe = NvmlGpuProbe()

    static = probe.static_gpus()
    status = probe.gpu_statuses()

    assert static[0].name == "NVIDIA RTX"
    assert static[0].vram_mb == 8192
    assert status[0].vram_free_mb == 6144
    assert status[0].util_percent == 42.0
    assert status[0].power_w == 120.0
    assert status[0].temp_c == 55.0


def test_nvml_probe_degrades_on_read_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probes_module, "_load_nvml", lambda: _fake_nvml(raise_on_read=True))
    probe = NvmlGpuProbe()

    assert probe.static_gpus() == ()
    assert probe.gpu_statuses() == ()
