"""fallow-modelmesh: content-addressed model distribution primitives.

The first increment (ADR 071) is the local, verifiable core: split a large model
file into content-addressed chunks, describe them in a signed manifest that is
the root of trust, hold chunks in a local size-capped store, and reconstruct the
file with full hash verification.

The second increment (ADR 072) adds the peer layer: discover which peers on the
tailnet hold which chunks, fetch a store's missing chunks from them with each
chunk verified against the signed manifest on receipt, and reconstruct through a
single entry point that gates on the signature and writes atomically. The
package still depends on the standard library only and is a leaf in the import
DAG (it imports no other Fallow package).
"""

from fallow_modelmesh.chunk import DEFAULT_CHUNK_SIZE, chunk_hash, iter_file_chunks
from fallow_modelmesh.delta import missing_chunks
from fallow_modelmesh.errors import (
    ChunkNotFound,
    ModelmeshError,
    VerificationError,
)
from fallow_modelmesh.exchange import fetch_delta
from fallow_modelmesh.manifest import Manifest, build_manifest
from fallow_modelmesh.merkle import merkle_root
from fallow_modelmesh.peer import Peer, PeerIndex, discover
from fallow_modelmesh.reconstruct import reconstruct
from fallow_modelmesh.safe import reconstruct_atomic, verified_reconstruct
from fallow_modelmesh.signing import sign_manifest, verify_manifest
from fallow_modelmesh.store import ChunkStore
from fallow_modelmesh.version import __version__

__all__ = [
    "DEFAULT_CHUNK_SIZE",
    "ChunkNotFound",
    "ChunkStore",
    "Manifest",
    "ModelmeshError",
    "Peer",
    "PeerIndex",
    "VerificationError",
    "__version__",
    "build_manifest",
    "chunk_hash",
    "discover",
    "fetch_delta",
    "iter_file_chunks",
    "merkle_root",
    "missing_chunks",
    "reconstruct",
    "reconstruct_atomic",
    "sign_manifest",
    "verified_reconstruct",
    "verify_manifest",
]
