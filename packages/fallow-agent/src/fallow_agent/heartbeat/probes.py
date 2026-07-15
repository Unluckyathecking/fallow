"""Hardware-probe seams.

Two ``Protocol`` seams isolate all platform I/O so metric collection is fully
deterministic in tests:

- :class:`SystemProbe` — CPU / memory / load / temperature, backed by ``psutil``
  and the stdlib ``platform``/``sys`` modules in :class:`PsutilSystemProbe`.
- :class:`GpuProbe` — static and dynamic GPU state, backed by NVML
  (``nvidia-ml-py``) in :class:`NvmlGpuProbe`. NVML is imported *lazily* and every
  failure degrades to an empty tuple, so machines without an NVIDIA GPU (or the
  library) never crash.
"""

from __future__ import annotations

import importlib
import logging
import platform
import sys
from pathlib import Path
from typing import Any, Protocol, cast

import psutil

from fallow_agent.heartbeat.constants import BYTES_PER_MB, NVIDIA_VENDOR
from fallow_protocol.capabilities import GpuInfo, GpuStatus, OsFamily

logger = logging.getLogger(__name__)

# psutil.disk_usage needs a real anchor; the user's home mount is always present.
_DISK_ANCHOR = str(Path.home())
_MILLIWATTS_PER_WATT = 1000.0


class SystemProbe(Protocol):
    """Reads host CPU / memory / load / temperature."""

    def hostname(self) -> str: ...
    def os_family(self) -> OsFamily: ...
    def os_version(self) -> str: ...
    def cpu_model(self) -> str: ...
    def cpu_cores(self) -> int: ...
    def ram_mb(self) -> int: ...
    def disk_free_mb(self) -> int: ...
    def cpu_percent(self) -> float: ...
    def mem_available_mb(self) -> int: ...
    def load_avg(self) -> float | None: ...
    def temp_cpu_c(self) -> float | None: ...


class GpuProbe(Protocol):
    """Reads static GPU inventory and dynamic per-GPU status."""

    def static_gpus(self) -> tuple[GpuInfo, ...]: ...
    def gpu_statuses(self) -> tuple[GpuStatus, ...]: ...


def _os_family() -> OsFamily:
    if sys.platform.startswith("win"):
        return OsFamily.WINDOWS
    if sys.platform == "darwin":
        return OsFamily.MACOS
    return OsFamily.LINUX


class PsutilSystemProbe:
    """Default :class:`SystemProbe` over ``psutil`` + stdlib ``platform``."""

    def __init__(self, disk_path: str = _DISK_ANCHOR) -> None:
        self._disk_path = disk_path

    def hostname(self) -> str:
        return platform.node()

    def os_family(self) -> OsFamily:
        return _os_family()

    def os_version(self) -> str:
        return platform.version()

    def cpu_model(self) -> str:
        return platform.processor()

    def cpu_cores(self) -> int:
        physical = psutil.cpu_count(logical=False)
        return physical if physical else (psutil.cpu_count() or 0)

    def ram_mb(self) -> int:
        return int(psutil.virtual_memory().total // BYTES_PER_MB)

    def disk_free_mb(self) -> int:
        return int(psutil.disk_usage(self._disk_path).free // BYTES_PER_MB)

    def cpu_percent(self) -> float:
        return float(psutil.cpu_percent())

    def mem_available_mb(self) -> int:
        return int(psutil.virtual_memory().available // BYTES_PER_MB)

    def load_avg(self) -> float | None:
        try:
            return float(psutil.getloadavg()[0])
        except (OSError, AttributeError):
            return None

    def temp_cpu_c(self) -> float | None:
        sensors = getattr(psutil, "sensors_temperatures", None)
        if sensors is None:
            return None
        try:
            readings = sensors()
        except (OSError, RuntimeError):
            return None
        for entries in readings.values():
            for entry in entries:
                if entry.current is not None:
                    return float(entry.current)
        return None


def _load_nvml() -> Any | None:
    """Import ``pynvml`` lazily; return ``None`` when it is unavailable."""
    try:
        return importlib.import_module("pynvml")
    except ImportError:
        return None


def _decode_name(name: Any) -> str:
    return name.decode() if isinstance(name, bytes) else str(name)


class NvmlGpuProbe:
    """Default :class:`GpuProbe` over NVML. Returns ``()`` when NVML is absent."""

    def static_gpus(self) -> tuple[GpuInfo, ...]:
        return cast("tuple[GpuInfo, ...]", self._with_nvml(self._read_static))

    def gpu_statuses(self) -> tuple[GpuStatus, ...]:
        return cast("tuple[GpuStatus, ...]", self._with_nvml(self._read_status))

    def _with_nvml(self, read: Any) -> Any:
        nvml = _load_nvml()
        if nvml is None:
            return ()
        try:
            nvml.nvmlInit()
        except Exception:
            logger.debug("NVML init failed; reporting no GPUs", exc_info=True)
            return ()
        try:
            return read(nvml)
        except Exception:
            logger.debug("NVML read failed; reporting no GPUs", exc_info=True)
            return ()
        finally:
            self._safe_shutdown(nvml)

    @staticmethod
    def _safe_shutdown(nvml: Any) -> None:
        try:
            nvml.nvmlShutdown()
        except Exception:
            logger.debug("NVML shutdown failed", exc_info=True)

    @staticmethod
    def _read_static(nvml: Any) -> tuple[GpuInfo, ...]:
        gpus: list[GpuInfo] = []
        for index in range(nvml.nvmlDeviceGetCount()):
            handle = nvml.nvmlDeviceGetHandleByIndex(index)
            mem = nvml.nvmlDeviceGetMemoryInfo(handle)
            gpus.append(
                GpuInfo(
                    index=index,
                    name=_decode_name(nvml.nvmlDeviceGetName(handle)),
                    vendor=NVIDIA_VENDOR,
                    vram_mb=int(mem.total // BYTES_PER_MB),
                )
            )
        return tuple(gpus)

    @classmethod
    def _read_status(cls, nvml: Any) -> tuple[GpuStatus, ...]:
        return tuple(
            cls._one_status(nvml, nvml.nvmlDeviceGetHandleByIndex(index), index)
            for index in range(nvml.nvmlDeviceGetCount())
        )

    @staticmethod
    def _one_status(nvml: Any, handle: Any, index: int) -> GpuStatus:
        mem = nvml.nvmlDeviceGetMemoryInfo(handle)
        util = nvml.nvmlDeviceGetUtilizationRates(handle)
        return GpuStatus(
            index=index,
            vram_free_mb=int(mem.free // BYTES_PER_MB),
            util_percent=float(util.gpu),
            power_w=_optional_power(nvml, handle),
            temp_c=_optional_temp(nvml, handle),
        )


def _optional_power(nvml: Any, handle: Any) -> float | None:
    try:
        return float(nvml.nvmlDeviceGetPowerUsage(handle)) / _MILLIWATTS_PER_WATT
    except Exception:
        return None


def _optional_temp(nvml: Any, handle: Any) -> float | None:
    try:
        return float(nvml.nvmlDeviceGetTemperature(handle, nvml.NVML_TEMPERATURE_GPU))
    except Exception:
        return None
