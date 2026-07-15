"""Admin-API request bodies (module I1, server half of ``docs/admin-api.md``).

These frozen models mirror the CLI's ``fallow_cli.models`` request shapes exactly.
They are **duplicated** here rather than imported because the coordinator must not
depend on ``fallow_cli`` (import-linter DAG); the shared contract is
``docs/admin-api.md``, and the committed JSON Schemas guard against drift.
"""

from __future__ import annotations

from pydantic import Field

from fallow_protocol.base import FallowModel
from fallow_protocol.models import ModelManifest


class ApiKeyRequest(FallowModel):
    """``POST /v1/admin/api_keys`` request body."""

    name: str
    model_allowlist: tuple[str, ...] | None = None
    rpm_limit: int | None = Field(default=None, strict=True, gt=0)
    daily_limit: int | None = Field(default=None, strict=True, gt=0)


class ModelRegisterRequest(FallowModel):
    """``POST /v1/admin/models`` request body (``blob_path`` is coordinator-local)."""

    manifest: ModelManifest
    blob_path: str


class AssignmentRequest(FallowModel):
    """``PUT /v1/admin/assignments`` request body (idempotent exact-set replace)."""

    model_id: str
    agent_ids: tuple[str, ...]
