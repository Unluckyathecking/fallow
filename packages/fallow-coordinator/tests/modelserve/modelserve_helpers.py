"""In-memory fakes for modelserve tests (no SqliteRegistry, no network)."""

from fallow_coordinator.registry import ModelRecord
from fallow_protocol.models import ModelManifest

VALID_TOKEN = "device-token-abc"
AGENT_ID = "agent-1"
SHA_ZERO = "0" * 64

# Deterministic 3000-byte fake model blob used across modelserve tests.
BLOB_BYTES = bytes((i * 7) % 256 for i in range(3000))


def make_manifest(model_id: str = "qwen2.5-7b") -> ModelManifest:
    return ModelManifest(
        model_id=model_id,
        family="qwen2.5",
        quant="Q4_K_M",
        file_name=f"{model_id}.gguf",
        sha256=SHA_ZERO,
        size_bytes=3000,
    )


class FakeBlobRegistry:
    """Structural :class:`BlobRegistry`: constant token + a model dictionary."""

    def __init__(self, models: dict[str, ModelRecord]) -> None:
        self._models = models

    async def authenticate_agent(self, bearer: str) -> str | None:
        return AGENT_ID if bearer == VALID_TOKEN else None

    async def get_manifest(self, model_id: str) -> ModelManifest | None:
        record = self._models.get(model_id)
        return None if record is None else record.manifest

    async def get_model(self, model_id: str) -> ModelRecord | None:
        return self._models.get(model_id)
