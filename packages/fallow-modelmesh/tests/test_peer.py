from fallow_modelmesh.peer import discover


class FakePeer:
    """A peer that only reports a fixed availability set."""

    def __init__(self, held: set[str]) -> None:
        self._held = frozenset(held)

    def available(self) -> frozenset[str]:
        return self._held

    def fetch(self, chunk_hash: str) -> bytes:  # pragma: no cover - unused here
        raise NotImplementedError


def test_index_maps_each_chunk_to_its_holders() -> None:
    a = FakePeer({"h1", "h2"})
    b = FakePeer({"h2", "h3"})
    index = discover([a, b])
    assert index.holders("h1") == (a,)
    assert index.holders("h2") == (a, b)
    assert index.holders("h3") == (b,)


def test_holders_follow_discovery_order() -> None:
    a = FakePeer({"shared"})
    b = FakePeer({"shared"})
    assert discover([b, a]).holders("shared") == (b, a)


def test_unknown_chunk_has_no_holders() -> None:
    assert discover([FakePeer({"h1"})]).holders("missing") == ()


def test_no_peers_gives_empty_index() -> None:
    assert discover([]).holders("anything") == ()
