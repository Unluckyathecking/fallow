from collections.abc import Callable

from fallow_modelmesh.topology import PeerMeta, order_peers, select_peer, topology_key


class FakePeer:
    """A stand-in peer identified by name; the transport methods are unused here."""

    def __init__(self, name: str) -> None:
        self.name = name

    def available(self) -> frozenset[str]:
        return frozenset()

    def fetch(self, chunk_hash: str) -> bytes:
        raise NotImplementedError


def _lookup(table: dict[str, PeerMeta]) -> Callable[[FakePeer], PeerMeta]:
    return lambda peer: table[peer.name]


def test_lan_peer_is_preferred_over_a_faster_wan_peer() -> None:
    lan = FakePeer("lan")
    wan = FakePeer("wan")
    meta = _lookup(
        {
            "lan": PeerMeta(lan=True, latency_ms=50.0, bandwidth_bps=1_000.0),
            "wan": PeerMeta(lan=False, latency_ms=5.0, bandwidth_bps=1_000_000.0),
        }
    )
    assert select_peer([wan, lan], meta) is lan


def test_among_wan_peers_lower_latency_wins() -> None:
    near = FakePeer("near")
    far = FakePeer("far")
    meta = _lookup(
        {
            "near": PeerMeta(lan=False, latency_ms=20.0, bandwidth_bps=1_000.0),
            "far": PeerMeta(lan=False, latency_ms=80.0, bandwidth_bps=9_000.0),
        }
    )
    assert select_peer([far, near], meta) is near


def test_latency_tie_breaks_to_higher_bandwidth() -> None:
    thin = FakePeer("thin")
    fat = FakePeer("fat")
    meta = _lookup(
        {
            "thin": PeerMeta(lan=True, latency_ms=10.0, bandwidth_bps=1_000.0),
            "fat": PeerMeta(lan=True, latency_ms=10.0, bandwidth_bps=8_000.0),
        }
    )
    assert select_peer([thin, fat], meta) is fat


def test_full_tie_keeps_discovery_order() -> None:
    first = FakePeer("first")
    second = FakePeer("second")
    same = PeerMeta(lan=True, latency_ms=10.0, bandwidth_bps=1_000.0)
    meta = _lookup({"first": same, "second": same})
    assert order_peers([first, second], meta) == (first, second)
    assert order_peers([second, first], meta) == (second, first)


def test_select_returns_none_with_no_holders() -> None:
    assert select_peer([], _lookup({})) is None


def test_order_ranks_all_holders() -> None:
    lan_fast = FakePeer("lan_fast")
    lan_slow = FakePeer("lan_slow")
    wan = FakePeer("wan")
    meta = _lookup(
        {
            "lan_fast": PeerMeta(lan=True, latency_ms=10.0, bandwidth_bps=1_000.0),
            "lan_slow": PeerMeta(lan=True, latency_ms=40.0, bandwidth_bps=1_000.0),
            "wan": PeerMeta(lan=False, latency_ms=5.0, bandwidth_bps=9_000.0),
        }
    )
    assert order_peers([wan, lan_slow, lan_fast], meta) == (lan_fast, lan_slow, wan)


def test_a_custom_policy_overrides_the_default() -> None:
    thin = FakePeer("thin")
    fat = FakePeer("fat")
    meta = _lookup(
        {
            "thin": PeerMeta(lan=True, latency_ms=10.0, bandwidth_bps=1_000.0),
            "fat": PeerMeta(lan=False, latency_ms=90.0, bandwidth_bps=9_000.0),
        }
    )
    # A policy that only cares about bandwidth, most first, ignoring LAN.
    by_bandwidth = lambda m: (0, 0.0, -m.bandwidth_bps)  # noqa: E731
    assert select_peer([thin, fat], meta) is thin
    assert select_peer([thin, fat], meta, policy=by_bandwidth) is fat


def test_default_key_encodes_lan_latency_bandwidth() -> None:
    assert topology_key(PeerMeta(lan=True, latency_ms=10.0, bandwidth_bps=1_000.0)) == (
        0,
        10.0,
        -1_000.0,
    )
    assert topology_key(PeerMeta(lan=False, latency_ms=10.0, bandwidth_bps=1_000.0))[0] == 1
