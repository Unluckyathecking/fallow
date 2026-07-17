"""Peer discovery: find which peers on the tailnet hold which chunks.

A worker learns what its peers have by asking each one for its chunk
availability set and folding the answers into a single index from chunk hash to
the peers that hold it. That is all discovery is here: an exchange of
availability maps over whatever transport the caller supplies, no gossip and no
peer-to-peer framework.

The ``Peer`` protocol is the transport seam. This package does not own the wire.
A production caller backs a peer with an HTTP client over the tailnet (ADR 006
already authenticates that channel); a test backs one with an in-memory fake.
Either way a peer is trusted for bytes only: every chunk it serves is checked
against the signed manifest before use (see ``exchange``), so a lying peer can
misreport what it holds but can never smuggle in a bad chunk.
"""

from collections.abc import Iterable
from typing import Protocol


class Peer(Protocol):
    """A remote chunk source reachable over the tailnet.

    ``available`` reports the chunk hashes the peer claims to hold, and
    ``fetch`` returns the bytes for one of them. Both are the caller's transport
    concern; this package only composes them.
    """

    def available(self) -> frozenset[str]:
        """Return the chunk hashes this peer currently holds."""
        ...

    def fetch(self, chunk_hash: str) -> bytes:
        """Return the bytes the peer holds under ``chunk_hash``."""
        ...


class PeerIndex:
    """Which peers hold which chunks, as of one discovery pass.

    The index is a frozen snapshot. Peers come and go, so it is rebuilt by
    calling ``discover`` again rather than mutated in place.
    """

    def __init__(self, holders: dict[str, tuple[Peer, ...]]) -> None:
        self._holders = dict(holders)

    def holders(self, chunk_hash: str) -> tuple[Peer, ...]:
        """Return the peers that reported holding ``chunk_hash``, in discovery
        order. Empty when no peer has it."""
        return self._holders.get(chunk_hash, ())


def discover(peers: Iterable[Peer]) -> PeerIndex:
    """Query each peer's availability and build the holder index.

    Peers are recorded in the order given, so ``holders`` returns a stable,
    deterministic preference list for each chunk.
    """
    holders: dict[str, list[Peer]] = {}
    for peer in peers:
        for chunk_hash in peer.available():
            holders.setdefault(chunk_hash, []).append(peer)
    return PeerIndex({h: tuple(ps) for h, ps in holders.items()})
