"""Device capability types: what a machine is, statically and right now."""

from enum import StrEnum

from pydantic import Field

from fallow_protocol.base import FallowModel


class OsFamily(StrEnum):
    WINDOWS = "windows"
    MACOS = "macos"
    LINUX = "linux"


class WorkerKind(StrEnum):
    """The kind of workload a model/worker serves."""

    CHAT = "chat"
    EMBED = "embed"
    TRANSCRIBE = "transcribe"


class GpuInfo(FallowModel):
    """Static description of one GPU, captured at registration."""

    index: int
    name: str
    vendor: str  # "nvidia" | "apple" | "amd" | "intel"
    vram_mb: int


class GpuStatus(FallowModel):
    """Dynamic per-GPU status, reported in every heartbeat."""

    index: int
    vram_free_mb: int
    util_percent: float = Field(ge=0, le=100)
    power_w: float | None = None
    temp_c: float | None = None


class DeviceCaps(FallowModel):
    """Static capabilities of a machine, captured at registration."""

    hostname: str
    os: OsFamily
    os_version: str
    cpu_model: str
    cpu_cores: int = Field(gt=0)
    ram_mb: int = Field(gt=0)
    disk_free_mb: int = Field(ge=0)
    gpus: tuple[GpuInfo, ...] = ()
    agent_version: str


class ModelRequirements(FallowModel):
    """What a work unit or replica needs from the machine that runs it."""

    model_id: str
    worker_kind: WorkerKind
    needs_gpu: bool = False
    min_ram_mb: int = 0
    min_vram_mb: int = 0
