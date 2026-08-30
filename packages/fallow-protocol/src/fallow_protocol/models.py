"""Model registry types: manifests and replica status."""

from enum import StrEnum

from pydantic import Field, model_validator

from fallow_protocol.base import FallowModel
from fallow_protocol.capabilities import WorkerKind


class ReplicaState(StrEnum):
    LOADING = "loading"
    READY = "ready"
    SUSPENDED = "suspended"
    STOPPED = "stopped"


def _cache_paths(file_name: str) -> frozenset[str]:
    """Every sibling path the agent cache reserves for one artifact.

    The blob itself, its ".part" partial download, and its ".sha256" marker —
    the on-disk layout both the Python and Go model caches share.
    """
    return frozenset({file_name, f"{file_name}.part", f"{file_name}.sha256"})


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
    # Optional multimodal projector companion (vision models). It is stored and
    # served beside the main blob and verified like it; all three fields are set
    # together or not at all.
    mmproj_file_name: str | None = None
    mmproj_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    mmproj_size_bytes: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _mmproj_all_or_none(self) -> "ModelManifest":
        fields = (self.mmproj_file_name, self.mmproj_sha256, self.mmproj_size_bytes)
        if any(f is not None for f in fields) and any(f is None for f in fields):
            raise ValueError(
                "mmproj_file_name, mmproj_sha256 and mmproj_size_bytes must be set together"
            )
        name = self.mmproj_file_name
        if name is not None:
            if not name or "/" in name or "\\" in name or name in {".", ".."}:
                # The companion resolves as a sibling of the main blob everywhere;
                # anything but a bare file name would escape that directory.
                raise ValueError("mmproj_file_name must be a bare file name")
            # Both artifacts are siblings, and the agent cache reserves three
            # paths for each: the file itself plus "<file>.part"/"<file>.sha256"
            # (see the modelcache store, mirrored in the Go agent). Any overlap
            # between the two sets is unusable in one direction or the other —
            # verification fails forever, llama-server is handed the model GGUF
            # as --mmproj, or a marker write lands on the other's bytes — so the
            # sets must be disjoint, not merely differently named.
            if _cache_paths(self.file_name) & _cache_paths(name):
                raise ValueError(
                    "mmproj_file_name must not collide with the model blob's cache paths"
                )
        if self.worker_kind is WorkerKind.OCR and self.mmproj_file_name is None:
            # OCR runs a llama-server vision replica, which needs the projector
            # on its command line; an OCR model without one can never serve a
            # page, so reject it at registration rather than launching blind.
            raise ValueError("ocr models require an mmproj projector companion")
        return self


class ReplicaStatus(FallowModel):
    """One running (or suspended) model replica on an agent."""

    model_id: str
    port: int
    state: ReplicaState
    inflight: int = 0
    gpu: bool = False  # holds VRAM; eligible for kill-escalation while suspended
