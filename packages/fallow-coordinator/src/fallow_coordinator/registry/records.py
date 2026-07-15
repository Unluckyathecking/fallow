"""Internal (non-wire) value objects returned by the registry."""

from datetime import datetime

from fallow_protocol.base import FallowModel
from fallow_protocol.models import ModelManifest


class ApiKeyInfo(FallowModel):
    """Resolved identity for an authenticated API key.

    ``model_allowlist`` of ``None`` means "all models" (admin / unrestricted);
    an empty tuple means the key may reach no models at all.
    """

    name: str
    key_id: str = ""
    model_allowlist: tuple[str, ...] | None = None
    rpm_limit: int | None = None
    daily_limit: int | None = None
    is_admin: bool = False


class ApiKeyQuotaSnapshot(FallowModel):
    """Persisted in-memory quota state for one client key."""

    key_id: str
    bucket_tokens: float
    bucket_updated_at: datetime
    day: str
    daily_count: int
    snapshotted_at: datetime


class ModelRecord(FallowModel):
    """A registered model row: its manifest, on-disk blob, and enabled flag.

    Consumed by the model-serving router to decide 404 vs. stream.
    """

    manifest: ModelManifest
    blob_path: str
    enabled: bool
