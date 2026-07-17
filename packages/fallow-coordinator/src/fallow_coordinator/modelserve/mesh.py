"""Coordinator as the root of trust for modelmesh distribution (ADR 074).

The coordinator already holds every registered model's blob. This module lets it
hand that blob to agents as a *signed manifest* plus per-chunk bytes, so a fleet
sharing one HMAC key can pull a model as verified chunks over the LAN instead of
each machine dragging the whole blob over the uplink.

Two things live here. :class:`MeshManifestBuilder` chunks a model file once, signs
the manifest with the shared key, and caches the result keyed by the blob's size
and mtime, so repeated requests do not re-read a multi-GB file. :func:`create_mesh_router`
exposes the two read endpoints an agent needs: the signed manifest, and one chunk
by its content hash. Both reuse the existing device-token auth, so only enrolled
agents reach them.

The signature is the whole point. A chunk an agent pulls from an untrusted peer is
only trusted because it hashes to something the coordinator's signed manifest
commits to. The coordinator signs; the agent verifies. This module never mutates
the modelmesh package; it composes its public API.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import anyio
from fastapi import APIRouter, Depends, Header, HTTPException, Response

from fallow_coordinator.httpauth import authenticate_agent
from fallow_coordinator.modelserve.blob import OCTET_STREAM
from fallow_coordinator.modelserve.protocols import BlobRegistry
from fallow_modelmesh import DEFAULT_CHUNK_SIZE, Manifest, build_manifest, sign_manifest

_UNKNOWN_MODEL = "unknown or disabled model"
_UNKNOWN_CHUNK = "chunk not part of this model"


def encode_signed_manifest(manifest: Manifest, signature: str) -> str:
    """Serialise a manifest and its detached signature to JSON for transport.

    The agent rebuilds the :class:`Manifest` from these fields and recomputes the
    signature over its canonical bytes, so only the field values have to survive
    the round trip, not this envelope's shape.
    """
    return json.dumps(
        {
            "manifest": {
                "model_id": manifest.model_id,
                "total_size": manifest.total_size,
                "chunk_size": manifest.chunk_size,
                "whole_file_sha256": manifest.whole_file_sha256,
                "chunks": list(manifest.chunks),
                "merkle_root": manifest.merkle_root,
            },
            "signature": signature,
        }
    )


class MeshManifestBuilder:
    """Build, sign, and cache a model's manifest, and read chunks by hash.

    The cache is keyed by the blob's (size, mtime): a re-registered model with new
    bytes rebuilds automatically, which is what makes delta upgrades work without
    any invalidation call.
    """

    def __init__(self, key: bytes, chunk_size: int = DEFAULT_CHUNK_SIZE) -> None:
        self._key = key
        self._chunk_size = chunk_size
        self._cache: dict[str, tuple[tuple[int, int], Manifest, str]] = {}

    def signed(self, model_id: str, blob_path: str) -> tuple[Manifest, str]:
        """Return the signed manifest for ``model_id``, building it if stale."""
        stat = os.stat(blob_path)
        fingerprint = (stat.st_size, stat.st_mtime_ns)
        cached = self._cache.get(model_id)
        if cached is not None and cached[0] == fingerprint:
            return cached[1], cached[2]
        manifest = build_manifest(Path(blob_path), model_id, self._chunk_size)
        signature = sign_manifest(manifest, self._key)
        self._cache[model_id] = (fingerprint, manifest, signature)
        return manifest, signature

    def chunk_bytes(self, model_id: str, blob_path: str, chunk_hash: str) -> bytes | None:
        """Return the bytes for ``chunk_hash`` in this model, or None if unknown.

        Fixed-size chunking makes the offset a multiple of the chunk size, so a
        content hash maps to a file offset through the ordered manifest list.
        """
        manifest, _ = self.signed(model_id, blob_path)
        try:
            index = manifest.chunks.index(chunk_hash)
        except ValueError:
            return None
        offset = index * manifest.chunk_size
        with open(blob_path, "rb") as handle:
            handle.seek(offset)
            return handle.read(manifest.chunk_size)


def create_mesh_router(registry: BlobRegistry, builder: MeshManifestBuilder) -> APIRouter:
    """Build the mesh router: signed manifest + per-chunk bytes, device-token auth."""
    router = APIRouter()

    async def require_agent(authorization: str | None = Header(default=None)) -> str:
        return await authenticate_agent(registry, authorization)

    async def _enabled_blob_path(model_id: str) -> str:
        record = await registry.get_model(model_id)
        if record is None or not record.enabled:
            raise HTTPException(status_code=404, detail=_UNKNOWN_MODEL)
        return record.blob_path

    @router.get("/v1/models/{model_id}/mesh/manifest")
    async def get_mesh_manifest(model_id: str, _agent_id: str = Depends(require_agent)) -> Response:
        blob_path = await _enabled_blob_path(model_id)
        manifest, signature = await anyio.to_thread.run_sync(builder.signed, model_id, blob_path)
        return Response(
            content=encode_signed_manifest(manifest, signature),
            media_type="application/json",
        )

    @router.get("/v1/models/{model_id}/mesh/chunk/{chunk_hash}")
    async def get_mesh_chunk(
        model_id: str, chunk_hash: str, _agent_id: str = Depends(require_agent)
    ) -> Response:
        blob_path = await _enabled_blob_path(model_id)
        data = await anyio.to_thread.run_sync(builder.chunk_bytes, model_id, blob_path, chunk_hash)
        if data is None:
            raise HTTPException(status_code=404, detail=_UNKNOWN_CHUNK)
        return Response(content=data, media_type=OCTET_STREAM)

    return router
