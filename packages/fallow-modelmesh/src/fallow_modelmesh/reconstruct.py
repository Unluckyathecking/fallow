"""Verified reconstruction of a file from a manifest and a chunk store.

Reconstruction is where trust is enforced. Every chunk is re-hashed as it is
read and checked against the hash the manifest commits to, and the whole file
is hashed and checked against the manifest's whole-file digest. Any mismatch
raises before the caller can use the output, so bytes from an untrusted peer
are never accepted on faith. The store guarantees a chunk matches its key on
write, but reconstruction re-checks anyway: a later disk- or peer-backed store
can return rotted bytes, and this is the layer that must catch it.

Verify the manifest signature first (see ``signing``); this function assumes
the manifest itself is trusted and enforces that the bytes match it.
"""

import hashlib
from pathlib import Path

from fallow_modelmesh.chunk import chunk_hash
from fallow_modelmesh.errors import VerificationError
from fallow_modelmesh.manifest import Manifest
from fallow_modelmesh.store import ChunkStore


def reconstruct(manifest: Manifest, store: ChunkStore, dest: Path) -> None:
    """Write the model file described by ``manifest`` to ``dest``.

    Raises ``ChunkNotFound`` if a required chunk is absent and
    ``VerificationError`` if any chunk hash, the total size, or the whole-file
    hash does not match the manifest.
    """
    whole = hashlib.sha256()
    written = 0
    with dest.open("wb") as handle:
        for expected in manifest.chunks:
            data = store.get(expected)
            if chunk_hash(data) != expected:
                raise VerificationError(f"chunk hash mismatch for {expected}")
            handle.write(data)
            whole.update(data)
            written += len(data)
    if written != manifest.total_size:
        raise VerificationError(
            f"size mismatch: wrote {written}, manifest says {manifest.total_size}"
        )
    if whole.hexdigest() != manifest.whole_file_sha256:
        raise VerificationError("whole-file hash mismatch")
