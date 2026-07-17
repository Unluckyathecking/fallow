"""Pick which peer to pull a chunk from when several hold it.

ADR 072 fetches from the first holder in discovery order, which ignores where
the holder sits. A peer on the same LAN is cheaper than one across the WAN, a
nearer peer answers sooner, and a peer with more spare bandwidth finishes
faster. This module orders holders by that preference and hands back the best.

The topology is injected. modelmesh does not measure latency or learn the LAN
layout; it is a leaf and must not import the agent or coordinator that do. The
caller supplies a ``metadata`` function mapping a peer to its ``PeerMeta``, so
tests pin the topology exactly and the leaf contract stays intact.

The policy is pluggable. A policy is a function from ``PeerMeta`` to a sort key;
``topology_key`` is the default and encodes the preference order LAN before WAN,
then lower latency, then higher bandwidth. Sorting is stable, so holders that
tie on the key keep their discovery order and selection stays deterministic.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from fallow_modelmesh.peer import Peer

SortKey = tuple[int, float, float]
Policy = Callable[["PeerMeta"], SortKey]


@dataclass(frozen=True)
class PeerMeta:
    """What the caller knows about a peer's place in the network.

    ``lan`` is whether the peer shares the local network, ``latency_ms`` is its
    measured round-trip time, and ``bandwidth_bps`` is the throughput it
    advertises for serving chunks.
    """

    lan: bool
    latency_ms: float
    bandwidth_bps: float


def topology_key(meta: PeerMeta) -> SortKey:
    """Default sort key: LAN before WAN, then lower latency, then higher bandwidth.

    Smaller keys sort first, so LAN maps to 0 and WAN to 1, latency is used as
    is, and bandwidth is negated so the largest comes first.
    """
    return (0 if meta.lan else 1, meta.latency_ms, -meta.bandwidth_bps)


def order_peers(
    holders: Sequence[Peer],
    metadata: Callable[[Peer], PeerMeta],
    policy: Policy = topology_key,
) -> tuple[Peer, ...]:
    """Return ``holders`` ordered most to least preferred under ``policy``.

    Ties keep discovery order because the sort is stable.
    """
    return tuple(sorted(holders, key=lambda peer: policy(metadata(peer))))


def select_peer(
    holders: Sequence[Peer],
    metadata: Callable[[Peer], PeerMeta],
    policy: Policy = topology_key,
) -> Peer | None:
    """Return the most preferred holder, or ``None`` when there are none.

    ``None`` lets the caller fall back, the same way an empty holder list from
    peer discovery does.
    """
    ordered = order_peers(holders, metadata, policy)
    return ordered[0] if ordered else None
