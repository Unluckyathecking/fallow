"""Chunk exchange: fetch a manifest's missing chunks from peers, verified.

This is the peer-layer counterpart to ``reconstruct``. It computes the chunks a
local store still needs for a target manifest (the delta set), pulls each one
from a peer that holds it, and checks the received bytes against the hash the
signed manifest commits to before letting them into the store. Bytes that do
not hash to the chunk they were requested as are rejected, never stored: a peer
is trusted for transport, never for content. This is the security invariant of
the whole peer layer.

Resume falls out of the delta computation. Chunks already in the store are not
in the delta set, so a call after an interruption fetches only what is still
missing. Because every accepted chunk is put into the store as it arrives, a
fetch that fails partway leaves the store holding the chunks it did get, and the
next call picks up from there.
"""

from fallow_modelmesh.chunk import chunk_hash
from fallow_modelmesh.delta import missing_chunks
from fallow_modelmesh.errors import ChunkNotFound, VerificationError
from fallow_modelmesh.manifest import Manifest
from fallow_modelmesh.peer import PeerIndex
from fallow_modelmesh.store import ChunkStore


def fetch_delta(manifest: Manifest, store: ChunkStore, index: PeerIndex) -> tuple[str, ...]:
    """Fetch into ``store`` the chunks ``manifest`` needs that it lacks.

    Each missing chunk is pulled from the first peer in ``index`` that holds it
    and verified against its manifest hash before being stored. Returns the
    chunk hashes fetched on this call, in fetch order.

    Raises ``ChunkNotFound`` if no peer holds a required chunk and
    ``VerificationError`` if a peer returns bytes that do not match the hash
    they were requested as.
    """
    fetched: list[str] = []
    for chunk in missing_chunks(manifest, store):
        holders = index.holders(chunk)
        if not holders:
            raise ChunkNotFound(chunk)
        data = holders[0].fetch(chunk)
        if chunk_hash(data) != chunk:
            raise VerificationError(f"peer served bytes that do not match chunk {chunk}")
        store.put(data)
        fetched.append(chunk)
    return tuple(fetched)
