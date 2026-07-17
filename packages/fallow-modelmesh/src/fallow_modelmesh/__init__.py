"""fallow-modelmesh: content-addressed model distribution primitives.

This is the first modelmesh increment (ADR 071): split a large model file into
content-addressed chunks, describe them in a signed manifest that is the root
of trust, hold chunks in a local size-capped store, and reconstruct the file
with full hash verification. There is no peer or network code here yet; that is
a later increment. The package depends on the standard library only and is a
leaf in the import DAG (it imports no other Fallow package).
"""

from fallow_modelmesh.chunk import DEFAULT_CHUNK_SIZE, chunk_hash, iter_file_chunks
from fallow_modelmesh.delta import missing_chunks
from fallow_modelmesh.errors import (
    ChunkNotFound,
    ModelmeshError,
    VerificationError,
)
from fallow_modelmesh.manifest import Manifest, build_manifest
from fallow_modelmesh.merkle import merkle_root
from fallow_modelmesh.reconstruct import reconstruct
from fallow_modelmesh.signing import sign_manifest, verify_manifest
from fallow_modelmesh.store import ChunkStore
from fallow_modelmesh.version import __version__

__all__ = [
    "DEFAULT_CHUNK_SIZE",
    "ChunkNotFound",
    "ChunkStore",
    "Manifest",
    "ModelmeshError",
    "VerificationError",
    "__version__",
    "build_manifest",
    "chunk_hash",
    "iter_file_chunks",
    "merkle_root",
    "missing_chunks",
    "reconstruct",
    "sign_manifest",
    "verify_manifest",
]
