from fallow_modelmesh.chunk import chunk_hash
from fallow_modelmesh.merkle import merkle_root


def test_order_matters() -> None:
    a, b = chunk_hash(b"a"), chunk_hash(b"b")
    assert merkle_root((a, b)) != merkle_root((b, a))


def test_odd_leaf_count_is_stable() -> None:
    hashes = tuple(chunk_hash(bytes([i])) for i in range(3))
    assert merkle_root(hashes) == merkle_root(hashes)


def test_root_changes_when_a_chunk_changes() -> None:
    base = tuple(chunk_hash(bytes([i])) for i in range(4))
    tampered = (base[0], chunk_hash(b"other"), base[2], base[3])
    assert merkle_root(base) != merkle_root(tampered)


def test_empty_root_is_defined() -> None:
    assert isinstance(merkle_root(()), str)
    assert len(merkle_root(())) == 64
