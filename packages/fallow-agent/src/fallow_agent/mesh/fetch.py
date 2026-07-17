"""The mesh fetch: verify a signed manifest, pull chunks, reconstruct.

This is the agent-side counterpart to the coordinator's ``modelserve.mesh``. It
composes the modelmesh public API and adds nothing to the trust model: the
signed manifest is the root of trust, every chunk a peer serves is checked
against a hash the manifest commits to, and reconstruction re-checks the whole
file before it lands on disk.

The three functions here are pure and synchronous so they test with in-memory
fakes and no network. :class:`~fallow_agent.mesh.store.MeshModelStore` runs them
in a worker thread and owns the HTTP transport and the fallback to the blob
download.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from fallow_agent.mesh.errors import MeshError
from fallow_agent.mesh.wire import parse_signed_manifest
from fallow_modelmesh import (
    ChunkStore,
    Manifest,
    Peer,
    discover,
    fetch_delta,
    iter_file_chunks,
    verified_reconstruct,
    verify_manifest,
)

# Cache artefacts that are not model blobs and must never be chunked for seeding.
_SEED_SKIP_SUFFIXES = frozenset({".part", ".partial", ".sha256", ".tmp"})


def verified_mesh_manifest(payload: Any, key: bytes, expected_sha256: str) -> tuple[Manifest, str]:
    """Parse and vet the coordinator's signed manifest before any fetch.

    Rejects, with :class:`MeshError`, a manifest whose signature does not verify
    under ``key`` (tampered or unsigned) or whose whole-file hash disagrees with
    the model manifest the reconcile loop already trusts. Either way the caller
    treats it as a mesh failure and falls back to the blob download, so a bad
    manifest can never reach reconstruction.
    """
    manifest, signature = parse_signed_manifest(payload)
    if not verify_manifest(manifest, signature, key):
        raise MeshError("mesh manifest signature does not verify")
    if manifest.whole_file_sha256 != expected_sha256:
        raise MeshError("mesh manifest sha256 disagrees with the model manifest")
    return manifest, signature


def seed_store_from_dir(store: ChunkStore, directory: Path, chunk_size: int) -> None:
    """Load chunks of any existing model blobs in ``directory`` into ``store``.

    This is what makes a delta upgrade fetch only changed chunks: the previous
    version's blob is still on disk, so its chunks seed the store and only the
    genuinely new chunks stay in the fetch set. Best effort by design; a file
    that cannot be read is skipped, costing only some redundant fetching.
    """
    if not directory.is_dir():
        return
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix in _SEED_SKIP_SUFFIXES:
            continue
        try:
            for data in iter_file_chunks(path, chunk_size):
                store.put(data)
        except (OSError, ValueError):
            continue


def fetch_and_reconstruct(
    manifest: Manifest,
    signature: str,
    key: bytes,
    peers: Sequence[Peer],
    store: ChunkStore,
    dest: Path,
) -> None:
    """Fetch the manifest's missing chunks from ``peers`` and reconstruct ``dest``.

    ``fetch_delta`` pulls only the chunks ``store`` lacks and verifies each on
    receipt; ``verified_reconstruct`` re-checks the signature and every hash and
    writes atomically. Both raise ``ModelmeshError`` subclasses on any mismatch,
    which the caller catches alongside :class:`MeshError` to fall back.
    """
    index = discover(peers)
    fetch_delta(manifest, store, index)
    verified_reconstruct(manifest, signature, key, store, dest)
