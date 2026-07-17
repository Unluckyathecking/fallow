"""HTTP transport for the mesh fetch: the coordinator as a chunk source.

The mesh logic in :mod:`fallow_agent.mesh.fetch` is pure and knows nothing about
HTTP. This module supplies it the two things it needs over the wire, both behind
the agent's existing device-token auth: the coordinator's signed manifest, and a
:class:`~fallow_modelmesh.Peer` that serves chunks from the coordinator's blob.

The coordinator holds the whole blob, so it can serve any chunk the manifest
lists; it is the always-available fallback source. LAN peers plug into the same
peer list ahead of it once peer discovery lands (ADR 072), and are preferred
because ``discover`` keeps peers in order. A peer is trusted for bytes only:
every chunk it returns is checked against the signed manifest before use.

The seam is a :class:`MeshSession` bound to one sync HTTP client, opened per
fetch by :meth:`HttpMeshTransport.session`. Fetches are rare (once per model, on
a cache miss), so a per-fetch client keeps lifecycle trivial and avoids holding a
second long-lived connection pool beside the agent's async client.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any, Protocol

import httpx

from fallow_agent.mesh.errors import MeshError
from fallow_modelmesh import Manifest, Peer

_MANIFEST_PATH = "/v1/models/{model_id}/mesh/manifest"
_CHUNK_PATH = "/v1/models/{model_id}/mesh/chunk/{chunk_hash}"
_DEFAULT_TIMEOUT_S = 30.0


class MeshSession(Protocol):
    """One fetch's worth of transport: the signed manifest and its chunk peers."""

    def signed_manifest(self, model_id: str) -> Any:
        """Return the decoded signed-manifest JSON payload for ``model_id``."""
        ...

    def peers(self, model_id: str, manifest: Manifest) -> Sequence[Peer]:
        """Return the chunk sources to fetch ``manifest`` from, most preferred first."""
        ...


class MeshTransport(Protocol):
    """Opens a :class:`MeshSession` for the duration of one mesh fetch."""

    def session(self) -> Any:
        """Return a context manager yielding a :class:`MeshSession`."""
        ...


class CoordinatorPeer:
    """A :class:`~fallow_modelmesh.Peer` backed by the coordinator's chunk endpoint.

    Reports every chunk the manifest lists as available, since the coordinator
    holds the whole blob, and fetches one chunk's bytes on demand.
    """

    def __init__(
        self, client: httpx.Client, base_url: str, model_id: str, chunks: frozenset[str]
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._model_id = model_id
        self._chunks = chunks

    def available(self) -> frozenset[str]:
        return self._chunks

    def fetch(self, chunk_hash: str) -> bytes:
        path = _CHUNK_PATH.format(model_id=self._model_id, chunk_hash=chunk_hash)
        response = self._client.get(f"{self._base_url}{path}")
        response.raise_for_status()
        return response.content


class HttpMeshSession:
    """A :class:`MeshSession` bound to one sync client and device token."""

    def __init__(self, client: httpx.Client, base_url: str) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")

    def signed_manifest(self, model_id: str) -> Any:
        path = _MANIFEST_PATH.format(model_id=model_id)
        response = self._client.get(f"{self._base_url}{path}")
        response.raise_for_status()
        try:
            return json.loads(response.content)
        except json.JSONDecodeError as exc:
            raise MeshError(f"signed manifest is not valid JSON: {exc}") from exc

    def peers(self, model_id: str, manifest: Manifest) -> Sequence[Peer]:
        coordinator = CoordinatorPeer(
            self._client, self._base_url, model_id, frozenset(manifest.chunks)
        )
        return [coordinator]


class HttpMeshTransport:
    """Production :class:`MeshTransport`: a sync client with the device token set."""

    def __init__(
        self, base_url: str, device_token: str, timeout_s: float = _DEFAULT_TIMEOUT_S
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {device_token}"}
        self._timeout_s = timeout_s

    @contextmanager
    def session(self) -> Iterator[MeshSession]:
        with httpx.Client(headers=self._headers, timeout=self._timeout_s) as client:
            yield HttpMeshSession(client, self._base_url)
