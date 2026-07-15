"""Model registry types: manifests and replica status."""

from enum import StrEnum

from pydantic import Field

from fallow_protocol.base import FallowModel
from fallow_protocol.capabilities import WorkerKind


class ReplicaState(StrEnum):
    LOADING = "loading"
    READY = "ready"
    SUSPENDED = "suspended"
    STOPPED = "stopped"


class ModelManifest(FallowModel):
    """A registered, approved model artifact.

    Workers download the blob from the coordinator and verify `sha256` before
    first use; the manifest is the only source of truth for how to launch it.
    """

    model_id: str  # registry name, e.g. "qwen2.5-7b-instruct-q4km"
    family: str  # e.g. "qwen2.5"
    quant: str  # e.g. "Q4_K_M"
    worker_kind: WorkerKind = WorkerKind.CHAT
    file_name: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)
    min_ram_mb: int = 0
    min_vram_mb: int = 0  # 0 = runs on CPU
    default_args: tuple[str, ...] = ()  # extra llama-server / worker args
    chat_template_hint: str | None = None
    license: str | None = None
    source_url: str | None = None


class ReplicaStatus(FallowModel):
    """One running (or suspended) model replica on an agent."""

    model_id: str
    port: int
    state: ReplicaState
    inflight: int = 0
