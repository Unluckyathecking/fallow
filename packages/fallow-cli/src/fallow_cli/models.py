"""Admin-API request/response bodies used only by the CLI.

These are the CLI's half of the admin-API contract specified in
``docs/admin-api.md``. Wave-3 implements the coordinator side against the same
shapes. They reuse :class:`fallow_protocol.FallowModel` so they are frozen and
reject unknown fields — protocol drift fails loudly at parse time.
"""

from __future__ import annotations

from pydantic import Field

from fallow_protocol import FallowModel, ModelManifest


class EnrollmentTokenResponse(FallowModel):
    """``POST /v1/admin/enrollment_tokens`` response body."""

    token: str


class ApiKeyRequest(FallowModel):
    """``POST /v1/admin/api_keys`` request body."""

    name: str
    model_allowlist: tuple[str, ...] | None = None
    rpm_limit: int | None = Field(default=None, strict=True, gt=0)
    daily_limit: int | None = Field(default=None, strict=True, gt=0)


class ApiKeyResponse(FallowModel):
    """``POST /v1/admin/api_keys`` response body."""

    key: str


class ModelRegisterRequest(FallowModel):
    """``POST /v1/admin/models`` request body.

    ``blob_path`` is a path on the coordinator host; v0.1 assumes the CLI runs
    on that host (see ``docs/admin-api.md``).
    """

    manifest: ModelManifest
    blob_path: str


class AssignmentRequest(FallowModel):
    """``PUT /v1/admin/assignments`` request body."""

    model_id: str
    agent_ids: tuple[str, ...]
