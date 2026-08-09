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


class SiteJoinBundle(FallowModel):
    """Strict v1 LAN Site join artifact."""

    version: int = Field(strict=True)
    site_id: str = Field(min_length=1, max_length=128)
    coordinator_urls: tuple[str, ...] = Field(min_length=1)
    coordinator_spki_sha256: tuple[str, ...] = Field(min_length=1)
    enrollment_token: str = Field(min_length=1)
    mdns_service: str | None

    @classmethod
    def model_validate(cls, obj: object, **kwargs: object) -> "SiteJoinBundle":
        result = super().model_validate(obj, **kwargs)
        if result.version != 1:
            raise ValueError("join bundle version must be 1")
        import re
        import base64
        if len(set(result.coordinator_urls)) != len(result.coordinator_urls):
            raise ValueError("join bundle contains duplicate coordinator URLs")
        for url in result.coordinator_urls:
            if not re.fullmatch(r"https://[^/?#]+(?::[0-9]+)?/?", url):
                raise ValueError("join bundle coordinator URLs must be HTTPS origins")
        if len(set(result.coordinator_spki_sha256)) != len(result.coordinator_spki_sha256):
            raise ValueError("join bundle contains duplicate certificate pins")
        for pin in result.coordinator_spki_sha256:
            encoded = pin.removeprefix("sha256/")
            if not pin.startswith("sha256/"):
                raise ValueError("join bundle pins must use sha256/ prefix")
            try:
                if len(base64.b64decode(encoded, validate=True)) != 32:
                    raise ValueError
            except Exception as exc:
                raise ValueError("join bundle pins must be SHA-256 base64 digests") from exc
        if result.mdns_service not in (None, "_fallow._tcp.local."):
            raise ValueError("invalid mDNS service")
        return result


class SiteJoinBundlesResponse(FallowModel):
    """Response from the coordinator's per-device join-bundle endpoint."""

    bundles: tuple[SiteJoinBundle, ...] = Field(min_length=1)
