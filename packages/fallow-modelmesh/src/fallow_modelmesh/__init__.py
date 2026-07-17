"""fallow-modelmesh: content-addressed model distribution primitives.

The first increment (ADR 071) is the local, verifiable core: split a large model
file into content-addressed chunks, describe them in a signed manifest that is
the root of trust, hold chunks in a local size-capped store, and reconstruct the
file with full hash verification.

The second increment (ADR 072) adds the peer layer: discover which peers on the
tailnet hold which chunks, fetch a store's missing chunks from them with each
chunk verified against the signed manifest on receipt, and reconstruct through a
single entry point that gates on the signature and writes atomically.

The third increment (ADR 073) adds two policy modules the peer layer can lean on
without changing it: a bandwidth limiter that paces transfer to a low rate while
the local user is active and full rate while idle, and topology-aware peer
selection that prefers a same-LAN, lower-latency, higher-bandwidth holder.

The fourth increment (ADR 075) adds offline seeding: export a model's signed
manifest and chunks to a portable bundle for USB or file-share transfer, then
import it on an air-gapped machine, verifying the signature and every chunk hash
before ingesting so the model reconstructs with no network. The package still
depends on the standard library only and is a leaf in the import DAG (it imports
no other Fallow package).
"""

from fallow_modelmesh.bandwidth import BandwidthLimiter
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
from fallow_modelmesh.offline import export_bundle, import_bundle
from fallow_modelmesh.peer import Peer, PeerIndex, discover
from fallow_modelmesh.reconstruct import reconstruct
from fallow_modelmesh.safe import reconstruct_atomic, verified_reconstruct
from fallow_modelmesh.signing import sign_manifest, verify_manifest
from fallow_modelmesh.store import ChunkStore
from fallow_modelmesh.topology import PeerMeta, order_peers, select_peer, topology_key
from fallow_modelmesh.version import __version__

__all__ = [
    "DEFAULT_CHUNK_SIZE",
    "BandwidthLimiter",
    "ChunkNotFound",
    "ChunkStore",
    "Manifest",
    "ModelmeshError",
    "Peer",
    "PeerIndex",
    "PeerMeta",
    "VerificationError",
    "__version__",
    "build_manifest",
    "chunk_hash",
    "discover",
    "export_bundle",
    "fetch_delta",
    "import_bundle",
    "iter_file_chunks",
    "merkle_root",
    "missing_chunks",
    "order_peers",
    "reconstruct",
    "reconstruct_atomic",
    "select_peer",
    "sign_manifest",
    "topology_key",
    "verified_reconstruct",
    "verify_manifest",
]
