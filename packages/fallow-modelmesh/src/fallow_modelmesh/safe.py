"""The one safe way to turn a signed manifest and a store into a file.

Two guarantees live here so a caller cannot skip either. First, the signature is
verified before any bytes are written, so an unsigned or tampered manifest can
never reach reconstruction. Second, reconstruction writes to a temporary path
next to the destination and renames it into place only on success, so a run that
fails partway (a missing or corrupt chunk) leaves no partial file behind for a
later reader to mistake for a complete model.

``reconstruct`` in the core module stays a plain, verifying reconstruction with
no filesystem ceremony. This wrapper adds the signature gate and the atomic
write around it, and is the entry point production code should call.
"""

from pathlib import Path

from fallow_modelmesh.errors import VerificationError
from fallow_modelmesh.manifest import Manifest
from fallow_modelmesh.reconstruct import reconstruct
from fallow_modelmesh.signing import verify_manifest
from fallow_modelmesh.store import ChunkStore


def reconstruct_atomic(manifest: Manifest, store: ChunkStore, dest: Path) -> None:
    """Reconstruct to a temp path and rename onto ``dest`` only on success.

    Any failure removes the temp file and re-raises, so ``dest`` is either the
    complete, verified file or untouched, never a partial write.
    """
    tmp = dest.with_name(dest.name + ".partial")
    try:
        reconstruct(manifest, store, tmp)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(dest)


def verified_reconstruct(
    manifest: Manifest,
    signature: str,
    key: bytes,
    store: ChunkStore,
    dest: Path,
) -> None:
    """Verify the manifest signature, then reconstruct atomically to ``dest``.

    Raises ``VerificationError`` if ``signature`` is not a valid signature over
    ``manifest`` under ``key``, before any bytes are written. Reconstruction
    then enforces the per-chunk and whole-file hashes as usual.
    """
    if not verify_manifest(manifest, signature, key):
        raise VerificationError("manifest signature does not verify")
    reconstruct_atomic(manifest, store, dest)
