"""``MeshModelStore``: an opt-in model store that fetches over the modelmesh.

Wraps the blob-download :class:`~fallow_agent.modelcache.HttpModelStore` and is
substituted for it only when the operator turns the mesh on. Presence checks and
the on-disk layout are the inner store's, unchanged, so a model fetched over the
mesh is indistinguishable on disk from one pulled as a blob and the heartbeat hot
path never knows the difference.

The contract is: try the mesh, and on any mesh failure fall back to the inner
blob download. A bad signature, a lying peer, an unreachable coordinator, a chunk
that will not verify — all collapse to "fetch it the old way". That is what keeps
this safe to enable: the mesh can only ever be faster, never a new way to fail.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

from fallow_agent.mesh.errors import MeshError
from fallow_agent.mesh.fetch import (
    fetch_and_reconstruct,
    seed_store_from_dir,
    verified_mesh_manifest,
)
from fallow_agent.mesh.transport import MeshTransport
from fallow_agent.modelcache.paths import blob_path, marker_path, write_marker_atomic
from fallow_modelmesh import ChunkStore, ModelmeshError
from fallow_protocol.interfaces import ModelStore
from fallow_protocol.models import ModelManifest

ToThread = Callable[..., Awaitable[None]]

# Mesh failures that mean "fall back to the blob download", never "crash".
_FALLBACK_ERRORS = (MeshError, ModelmeshError, httpx.HTTPError)


class MeshModelStore(ModelStore):
    """Mesh-first model store with a blob-download fallback."""

    def __init__(
        self,
        *,
        inner: ModelStore,
        transport: MeshTransport,
        signing_key: bytes,
        cache_dir: Path,
        store_capacity_bytes: int,
        to_thread: ToThread = asyncio.to_thread,
    ) -> None:
        self._inner = inner
        self._transport = transport
        self._key = signing_key
        self._cache_dir = cache_dir.expanduser()
        self._capacity = store_capacity_bytes
        self._to_thread = to_thread
        self._locks: dict[str, asyncio.Lock] = {}

    def path_if_present(self, manifest: ModelManifest) -> Path | None:
        """Delegate: a mesh-built blob is published exactly like a downloaded one."""
        return self._inner.path_if_present(manifest)

    async def ensure(self, manifest: ModelManifest) -> Path:
        """Return a verified local path, fetching over the mesh with blob fallback."""
        present = self.path_if_present(manifest)
        if present is not None:
            return present
        async with self._lock_for(manifest.model_id):
            present = self.path_if_present(manifest)
            if present is not None:
                return present
            dest = blob_path(self._cache_dir, manifest)
            try:
                await self._to_thread(self._fetch_over_mesh, manifest, dest)
            except _FALLBACK_ERRORS:
                return await self._inner.ensure(manifest)
            return dest

    def _lock_for(self, model_id: str) -> asyncio.Lock:
        # Safe without a guard: dict get/set has no await between them.
        lock = self._locks.get(model_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[model_id] = lock
        return lock

    def _fetch_over_mesh(self, manifest: ModelManifest, dest: Path) -> None:
        """Blocking mesh fetch (run in a worker thread): verify, pull, reconstruct.

        Reuses the inner store's on-disk layout so the published blob and its
        verification marker match a downloaded one byte for byte.
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self._transport.session() as session:
            payload = session.signed_manifest(manifest.model_id)
            mesh_manifest, signature = verified_mesh_manifest(payload, self._key, manifest.sha256)
            store = ChunkStore(self._capacity)
            seed_store_from_dir(store, dest.parent, mesh_manifest.chunk_size)
            peers = session.peers(manifest.model_id, mesh_manifest)
            fetch_and_reconstruct(mesh_manifest, signature, self._key, peers, store, dest)
        write_marker_atomic(marker_path(self._cache_dir, manifest), manifest.sha256)
