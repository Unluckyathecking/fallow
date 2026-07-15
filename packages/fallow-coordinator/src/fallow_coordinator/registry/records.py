"""Internal (non-wire) value objects returned by the registry."""

from fallow_protocol.base import FallowModel
from fallow_protocol.models import ModelManifest


class ApiKeyInfo(FallowModel):
    """Resolved identity for an authenticated API key.

    ``model_allowlist`` of ``None`` means "all models" (admin / unrestricted);
    an empty tuple means the key may reach no models at all.
    """

    name: str
    model_allowlist: tuple[str, ...] | None = None
    is_admin: bool = False


class ModelRecord(FallowModel):
    """A registered model row: its manifest, on-disk blob, and enabled flag.

    Consumed by the model-serving router to decide 404 vs. stream.
    """

    manifest: ModelManifest
    blob_path: str
    enabled: bool
