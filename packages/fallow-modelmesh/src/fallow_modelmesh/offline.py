"""Offline seeding: export a model to a bundle, import and verify it back.

This is the peer layer's air-gapped counterpart. Where ``exchange`` pulls chunks
over the LAN, offline seeding writes them to a directory a USB stick or file
share can carry to a machine with no network at all. Export packs the signed
manifest and the model's chunks into a bundle; import reads the bundle on the
target, verifies it, and fills the local store so the model reconstructs offline.

Import enforces the same security invariant as the rest of the package, in
order. The manifest signature is checked first, so an unsigned or tampered
manifest is rejected before a single byte is ingested. Then each chunk is
re-hashed and checked against the hash the signed manifest commits to before it
enters the store, so tampered bytes never get in. A bundle is trusted for
transport, never for content.

Resume falls out of the delta set, exactly as it does for peer fetch. Import
only reads and ingests the chunks the store still lacks, so a run interrupted
partway leaves the store holding what it got, and a re-run skips those and
fetches the rest.
"""

from pathlib import Path

from fallow_modelmesh import bundle
from fallow_modelmesh.chunk import chunk_hash
from fallow_modelmesh.delta import missing_chunks
from fallow_modelmesh.errors import ChunkNotFound, VerificationError
from fallow_modelmesh.manifest import Manifest
from fallow_modelmesh.signing import verify_manifest
from fallow_modelmesh.store import ChunkStore


def export_bundle(manifest: Manifest, signature: str, store: ChunkStore, dest: Path) -> None:
    """Write ``manifest``, its ``signature``, and its chunks to a bundle at ``dest``.

    Every distinct chunk the manifest lists is pulled from ``store`` and written
    once, keyed by its content address. Raises ``ChunkNotFound`` if the store is
    missing a chunk the manifest requires.
    """
    chunks = dest / bundle.CHUNKS_DIR
    chunks.mkdir(parents=True, exist_ok=True)
    (dest / bundle.MANIFEST_NAME).write_bytes(manifest.canonical_bytes())
    (dest / bundle.SIGNATURE_NAME).write_text(signature, encoding="utf-8")
    for h in dict.fromkeys(manifest.chunks):
        bundle.chunk_path(dest, h).write_bytes(store.get(h))


def import_bundle(src: Path, key: bytes, store: ChunkStore) -> Manifest:
    """Verify the bundle at ``src`` and ingest its chunks into ``store``.

    The manifest signature is verified under ``key`` before anything is
    ingested; a bad signature raises ``VerificationError`` and touches nothing.
    Then each chunk the store still lacks is read from the bundle, checked
    against its manifest hash, and stored, so a re-run skips chunks already
    present. Returns the verified manifest, from which the caller reconstructs
    offline.

    Raises ``VerificationError`` if the signature or any chunk hash fails, and
    ``ChunkNotFound`` if a needed chunk is absent from the bundle.
    """
    manifest = bundle.parse_manifest((src / bundle.MANIFEST_NAME).read_bytes())
    signature = (src / bundle.SIGNATURE_NAME).read_text(encoding="utf-8").strip()
    if not verify_manifest(manifest, signature, key):
        raise VerificationError("bundle manifest signature does not verify")
    for h in missing_chunks(manifest, store):
        path = bundle.chunk_path(src, h)
        if not path.is_file():
            raise ChunkNotFound(h)
        data = path.read_bytes()
        if chunk_hash(data) != h:
            raise VerificationError(f"bundle chunk {h} does not match its hash")
        store.put(data)
    return manifest
