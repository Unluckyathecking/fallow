"""Assemble wire-ready capability and heartbeat metrics from the probe seams.

``collect_caps`` runs once at registration; ``collect_heartbeat_metrics`` runs
every heartbeat. Both take injectable :class:`SystemProbe` / :class:`GpuProbe`
seams (defaulting to the real psutil/NVML implementations) so tests never touch
hardware.
"""

from __future__ import annotations

from typing import NamedTuple

from fallow_agent.heartbeat.constants import (
    MAX_CPU_PERCENT,
    MIN_CPU_CORES,
    MIN_CPU_PERCENT,
    UNKNOWN_CPU_MODEL,
)
from fallow_agent.heartbeat.probes import (
    GpuProbe,
    NvmlGpuProbe,
    PsutilSystemProbe,
    SystemProbe,
)
from fallow_protocol.capabilities import DeviceCaps, GpuStatus


class HeartbeatMetrics(NamedTuple):
    """Dynamic host metrics sampled once per heartbeat.

    Field order matches the module contract:
    ``(cpu_percent, mem_available_mb, load_avg, gpus, temp_cpu_c)``.
    """

    cpu_percent: float
    mem_available_mb: int
    load_avg: float | None
    gpus: tuple[GpuStatus, ...]
    temp_cpu_c: float | None


def _clamp_percent(value: float) -> float:
    return min(MAX_CPU_PERCENT, max(MIN_CPU_PERCENT, value))


def collect_caps(
    agent_version: str,
    *,
    system: SystemProbe | None = None,
    gpu: GpuProbe | None = None,
) -> DeviceCaps:
    """Capture the machine's static capabilities for registration."""
    system = system or PsutilSystemProbe()
    gpu = gpu or NvmlGpuProbe()
    return DeviceCaps(
        hostname=system.hostname(),
        os=system.os_family(),
        os_version=system.os_version(),
        cpu_model=system.cpu_model() or UNKNOWN_CPU_MODEL,
        cpu_cores=max(MIN_CPU_CORES, system.cpu_cores()),
        ram_mb=system.ram_mb(),
        disk_free_mb=system.disk_free_mb(),
        gpus=gpu.static_gpus(),
        agent_version=agent_version,
    )


def collect_heartbeat_metrics(
    *,
    system: SystemProbe | None = None,
    gpu: GpuProbe | None = None,
) -> HeartbeatMetrics:
    """Sample the dynamic metrics that ride in every heartbeat."""
    system = system or PsutilSystemProbe()
    gpu = gpu or NvmlGpuProbe()
    return HeartbeatMetrics(
        cpu_percent=_clamp_percent(system.cpu_percent()),
        mem_available_mb=system.mem_available_mb(),
        load_avg=system.load_avg(),
        gpus=gpu.gpu_statuses(),
        temp_cpu_c=system.temp_cpu_c(),
    )
