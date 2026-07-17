import pytest

from fallow_modelmesh.errors import ChunkNotFound
from fallow_modelmesh.store import ChunkStore


def test_put_dedups_identical_bytes() -> None:
    store = ChunkStore(max_bytes=1000)
    first = store.put(b"hello")
    second = store.put(b"hello")
    assert first == second
    assert store.used_bytes == 5


def test_get_returns_stored_bytes() -> None:
    store = ChunkStore(max_bytes=1000)
    key = store.put(b"payload")
    assert store.get(key) == b"payload"


def test_get_missing_raises() -> None:
    store = ChunkStore(max_bytes=1000)
    with pytest.raises(ChunkNotFound):
        store.get("deadbeef")


def test_has_and_availability() -> None:
    store = ChunkStore(max_bytes=1000)
    key = store.put(b"data")
    assert store.has(key) is True
    assert store.availability() == frozenset({key})


def test_lru_eviction_drops_oldest() -> None:
    store = ChunkStore(max_bytes=20)
    a = store.put(b"0123456789")  # 10 bytes
    b = store.put(b"abcdefghij")  # 10 bytes, store now full
    store.put(b"ABCDEFGHIJ")  # 10 bytes, evicts the oldest (a)
    assert store.has(a) is False
    assert store.has(b) is True


def test_access_refreshes_recency() -> None:
    store = ChunkStore(max_bytes=20)
    a = store.put(b"0123456789")
    b = store.put(b"abcdefghij")
    store.get(a)  # a is now most-recently-used
    store.put(b"ABCDEFGHIJ")  # evicts b instead
    assert store.has(a) is True
    assert store.has(b) is False


def test_chunk_larger_than_cap_rejected() -> None:
    store = ChunkStore(max_bytes=4)
    with pytest.raises(ValueError, match="capacity"):
        store.put(b"toolong")
