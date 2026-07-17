"""Merkle root over an ordered list of chunk hashes.

The root is a single hash that commits to every chunk and to their order, so a
signature over the manifest (which carries the root) vouches for the whole
chunk list at once. Leaves and internal nodes are domain-separated with a
one-byte prefix so a leaf digest can never be replayed as an internal node
(second-preimage hardening). An odd node at any level is promoted unchanged
rather than duplicated.
"""

import hashlib

_LEAF = b"\x00"
_NODE = b"\x01"


def _leaf(hex_hash: str) -> bytes:
    return hashlib.sha256(_LEAF + bytes.fromhex(hex_hash)).digest()


def _node(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(_NODE + left + right).digest()


def merkle_root(chunk_hashes: tuple[str, ...]) -> str:
    """Return the Merkle root hex digest for ``chunk_hashes`` in order.

    An empty list has a fixed root (the digest of the leaf prefix over no
    bytes) so an empty file still gets a well-defined, verifiable manifest.
    """
    if not chunk_hashes:
        return hashlib.sha256(_LEAF).hexdigest()
    level = [_leaf(h) for h in chunk_hashes]
    while len(level) > 1:
        nxt: list[bytes] = []
        for i in range(0, len(level), 2):
            if i + 1 < len(level):
                nxt.append(_node(level[i], level[i + 1]))
            else:
                nxt.append(level[i])
        level = nxt
    return level[0].hex()
