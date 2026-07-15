"""Upload work-unit result bytes to the coordinator with integrity checking."""

from __future__ import annotations

import hashlib
from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from fallow_agent.heartbeat.constants import LEASE_ATTEMPT_HEADER
from fallow_protocol.messages import WorkUnitLease

RESULT_PAYLOAD_PATH_TEMPLATE = "/v1/agents/{agent_id}/work_units/{unit_id}/payload"
_HTTP_OK = 200


class ResultUploadError(Exception):
    """A result payload could not be accepted by the coordinator."""


class ResultUploadTransientError(ResultUploadError):
    """An upload failed in a way that can be retried after lease expiry."""


class ResultUploadDigestMismatch(ResultUploadTransientError):
    """The coordinator returned a digest different from the uploaded bytes."""


class _UploadResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    result_ref: str


class ResultUploader:
    """Typed result writer over an injected ``httpx.AsyncClient``."""

    def __init__(
        self,
        *,
        base_url: str,
        agent_id: str,
        device_token: str,
        client: httpx.AsyncClient,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._agent_id = agent_id
        self._client = client
        self._headers = {"Authorization": f"Bearer {device_token}"}

    async def upload(self, lease: WorkUnitLease, payload: bytes) -> str:
        """Upload ``payload`` and return its verified lowercase SHA-256 ref."""
        expected = hashlib.sha256(payload).hexdigest()
        url = self._base + RESULT_PAYLOAD_PATH_TEMPLATE.format(
            agent_id=self._agent_id, unit_id=lease.work_unit_id
        )
        headers = {**self._headers, LEASE_ATTEMPT_HEADER: str(lease.attempt)}
        try:
            response = await self._client.post(url, headers=headers, content=payload)
        except httpx.TransportError as exc:
            raise ResultUploadTransientError(
                f"result upload for {lease.work_unit_id!r} failed: {exc}"
            ) from exc
        if response.status_code != _HTTP_OK:
            error_type = (
                ResultUploadTransientError if response.status_code >= 500 else ResultUploadError
            )
            raise error_type(
                f"result upload for {lease.work_unit_id!r} returned HTTP {response.status_code}"
            )
        try:
            result_ref = _UploadResponse.model_validate_json(response.content).result_ref
        except ValidationError as exc:
            raise ResultUploadError(
                f"malformed result upload response for {lease.work_unit_id!r}: {exc}"
            ) from exc
        if result_ref != expected:
            raise ResultUploadDigestMismatch(
                f"result upload digest mismatch: expected {expected}, got {result_ref}"
            )
        return result_ref


class ResultUploaderLike(Protocol):
    """Structural seam used by the runner wiring and its tests."""

    async def upload(self, lease: WorkUnitLease, payload: bytes) -> str: ...
