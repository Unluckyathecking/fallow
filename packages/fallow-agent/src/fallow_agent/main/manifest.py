"""Fetch a :class:`ModelManifest` from the coordinator.

The model cache (A4) pulls the *blob*; the runtime needs the *manifest* (how to
launch the model) before it can start a replica. Rather than widen the A5 client
(ADR 009 keeps its surface minimal), this is a tiny typed GET reusing the same
``base_url`` and device token the rest of the runtime already holds.

Endpoint (ADR 007): ``GET /v1/models/{model_id}/manifest`` — device-token auth,
``200`` with the manifest JSON body, ``404`` for an unknown model.
"""

from __future__ import annotations

import httpx
from pydantic import ValidationError

from fallow_agent.main.errors import ManifestFetchError
from fallow_protocol.models import ModelManifest

MANIFEST_PATH_TEMPLATE = "/v1/models/{model_id}/manifest"
_HTTP_OK = 200


class ManifestFetcher:
    """Typed manifest reader over an injected ``httpx.AsyncClient``."""

    def __init__(self, *, base_url: str, device_token: str, client: httpx.AsyncClient) -> None:
        self._base = base_url.rstrip("/")
        self._client = client
        self._headers = {"Authorization": f"Bearer {device_token}"}

    async def fetch(self, model_id: str) -> ModelManifest:
        """Return the manifest for ``model_id`` or raise :class:`ManifestFetchError`."""
        url = self._base + MANIFEST_PATH_TEMPLATE.format(model_id=model_id)
        try:
            resp = await self._client.get(url, headers=self._headers)
        except httpx.HTTPError as exc:
            raise ManifestFetchError(f"manifest fetch for {model_id!r} failed: {exc}") from exc
        if resp.status_code != _HTTP_OK:
            raise ManifestFetchError(
                f"manifest fetch for {model_id!r} returned HTTP {resp.status_code}"
            )
        try:
            return ModelManifest.model_validate_json(resp.content)
        except ValidationError as exc:
            raise ManifestFetchError(f"malformed manifest for {model_id!r}: {exc}") from exc
