"""Admin-API request/response bodies used only by the CLI.

These are the CLI's half of the admin-API contract specified in
``docs/admin-api.md``. Wave-3 implements the coordinator side against the same
shapes. They reuse :class:`fallow_protocol.FallowModel` so they are frozen and
reject unknown fields — protocol drift fails loudly at parse time.
"""

from __future__ import annotations

import base64
import re
from datetime import datetime
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from fallow_protocol import FallowModel, ModelManifest

# Canonical certificate-pin spelling: "sha256/" + standard base64 of a 32-byte
# digest (43 payload chars + one "="). Kept byte-for-byte in step with the Go
# site-client's decodePin so a bundle this CLI writes always parses there.
_SPKI_PIN_RE = re.compile(r"sha256/[A-Za-z0-9+/]{43}=")


def _validate_https_origin(url: str) -> None:
    """Accept only a bare HTTPS origin, matching the Go client's URL check.

    Requires the ``https`` scheme and a host, and forbids userinfo, a path
    beyond ``/``, any query or fragment, and out-of-range ports. A greedy regex
    silently accepts ``https://host:70000`` and ``https://user@host``, so the
    URL is parsed structurally instead.
    """
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or "?" in url
        or "#" in url
        or parts.path not in ("", "/")
    ):
        raise ValueError("join bundle coordinator URLs must be HTTPS origins")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("join bundle coordinator URLs must be HTTPS origins") from exc
    if port is not None and not (1 <= port <= 65535):
        raise ValueError("join bundle coordinator URLs must be HTTPS origins")


def _validate_spki_pin(pin: str) -> None:
    """Accept only a canonical ``sha256/<base64>`` 32-byte digest.

    Beyond decoding to 32 bytes, the payload must re-encode to itself so that
    non-canonical trailing bits (which some base64 decoders tolerate) are
    rejected, exactly as the Go site-client does.
    """
    if not _SPKI_PIN_RE.fullmatch(pin):
        raise ValueError("join bundle pins must be canonical sha256/ base64 digests")
    payload = pin.removeprefix("sha256/")
    try:
        raw = base64.b64decode(payload, validate=True)
    except ValueError as exc:
        raise ValueError("join bundle pins must be canonical sha256/ base64 digests") from exc
    if len(raw) != 32 or base64.b64encode(raw).decode() != payload:
        raise ValueError("join bundle pins must be canonical sha256/ base64 digests")


class EnrollmentTokenResponse(FallowModel):
    """``POST /v1/admin/enrollment_tokens`` response body."""

    token: str


class EnrollmentTokenInfo(FallowModel):
    """One row of ``GET /v1/admin/enrollment_tokens`` (never the token itself)."""

    token_id: str
    mode: str
    state: str
    created_at: datetime


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

    @model_validator(mode="after")
    def validate_contract(self) -> SiteJoinBundle:
        if self.version != 1:
            raise ValueError("join bundle version must be 1")
        if len(set(self.coordinator_urls)) != len(self.coordinator_urls):
            raise ValueError("join bundle contains duplicate coordinator URLs")
        for url in self.coordinator_urls:
            _validate_https_origin(url)
        if len(set(self.coordinator_spki_sha256)) != len(self.coordinator_spki_sha256):
            raise ValueError("join bundle contains duplicate certificate pins")
        for pin in self.coordinator_spki_sha256:
            _validate_spki_pin(pin)
        if self.mdns_service not in (None, "_fallow._tcp.local."):
            raise ValueError("invalid mDNS service")
        return self


class SiteJoinBundlesResponse(FallowModel):
    """Response from the coordinator's per-device join-bundle endpoint."""

    bundles: tuple[SiteJoinBundle, ...] = Field(min_length=1)
