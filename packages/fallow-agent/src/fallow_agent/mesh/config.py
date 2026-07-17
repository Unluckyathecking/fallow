"""Configuration for the opt-in mesh model-distribution path (ADR 074).

Off by default. When ``enabled``, the agent fetches a model as verified chunks
over the modelmesh instead of downloading the whole blob, falling back to the
blob download on any mesh failure. ``signing_key`` is the shared HMAC key the
coordinator signs manifests with and the agent verifies them against; it is the
same secret on both sides, handed out over the already-authenticated enrolment
channel (ADR 006).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Chunks are held in memory during reconstruction, so the cap bounds the mesh
# path to models that fit it; larger models simply fall back to the blob
# download. 2 GiB by default.
_DEFAULT_STORE_CAPACITY_BYTES = 2 * 1024 * 1024 * 1024


class MeshSettings(BaseModel):
    """Opt-in modelmesh distribution knobs. Disabled unless ``enabled`` is set."""

    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    signing_key: str | None = None
    store_capacity_bytes: int = Field(default=_DEFAULT_STORE_CAPACITY_BYTES, gt=0)

    @model_validator(mode="after")
    def _key_required_when_enabled(self) -> MeshSettings:
        if self.enabled and not self.signing_key:
            raise ValueError("mesh.enabled requires mesh.signing_key")
        return self
