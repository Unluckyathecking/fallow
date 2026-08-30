"""In-memory fakes for modelserve tests (no SqliteRegistry, no network)."""

from fallow_coordinator.registry import ModelRecord
from fallow_protocol.models import ModelManifest

VALID_TOKEN = "device-token-abc"
AGENT_ID = "agent-1"
SHA_ZERO = "0" * 64

# Deterministic 3000-byte fake model blob used across modelserve tests.
BLOB_BYTES = bytes((i * 7) % 256 for i in range(3000))
# Deterministic 1200-byte fake mmproj companion blob.
MMPROJ_BYTES = bytes((i * 11) % 256 for i in range(1200))
MMPROJ_FILE_NAME = "mmproj.gguf"


def make_manifest(model_id: str = "qwen2.5-7b", *, mmproj: bool = False) -> ModelManifest:
    return ModelManifest(
        model_id=model_id,
        family="qwen2.5",
        quant="Q4_K_M",
        file_name=f"{model_id}.gguf",
        sha256=SHA_ZERO,
        size_bytes=3000,
        mmproj_file_name=MMPROJ_FILE_NAME if mmproj else None,
        mmproj_sha256=SHA_ZERO if mmproj else None,
        mmproj_size_bytes=len(MMPROJ_BYTES) if mmproj else None,
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
