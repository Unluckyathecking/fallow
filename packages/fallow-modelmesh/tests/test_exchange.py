from pathlib import Path

import pytest

from fallow_modelmesh.chunk import iter_file_chunks
from fallow_modelmesh.errors import ChunkNotFound, VerificationError
from fallow_modelmesh.exchange import fetch_delta
from fallow_modelmesh.manifest import Manifest, build_manifest
from fallow_modelmesh.peer import discover
from fallow_modelmesh.reconstruct import reconstruct
from fallow_modelmesh.store import ChunkStore

DATA = b"gguf" + bytes(range(256)) * 8


class FakePeer:
    """An in-memory peer serving chunks from a hash->bytes map.

    ``tamper`` maps a chunk hash to the wrong bytes it should return instead, to
    stand in for a dishonest or corrupt peer. ``fail_after`` raises a transport
    error once that many fetches have succeeded, to stand in for a dropped
    connection mid-transfer.
    """

    def __init__(
        self,
        holdings: dict[str, bytes],
        *,
        tamper: dict[str, bytes] | None = None,
        fail_after: int | None = None,
    ) -> None:
        self._holdings = dict(holdings)
        self._tamper = tamper or {}
        self._fail_after = fail_after
        self.fetched: list[str] = []

    def available(self) -> frozenset[str]:
        return frozenset(self._holdings)

    def fetch(self, chunk_hash: str) -> bytes:
        if self._fail_after is not None and len(self.fetched) >= self._fail_after:
            raise RuntimeError("connection dropped")
        self.fetched.append(chunk_hash)
        if chunk_hash in self._tamper:
            return self._tamper[chunk_hash]
        return self._holdings[chunk_hash]


def _fixture(tmp_path: Path) -> tuple[Manifest, dict[str, bytes]]:
    src = tmp_path / "model.gguf"
    src.write_bytes(DATA)
    manifest = build_manifest(src, model_id="m1", chunk_size=64)
    holdings = dict(zip(manifest.chunks, iter_file_chunks(src, 64), strict=True))
    return manifest, holdings


def test_fetches_only_the_delta_set(tmp_path: Path) -> None:
    manifest, holdings = _fixture(tmp_path)
    store = ChunkStore(max_bytes=100_000)
    already = manifest.chunks[0]
    store.put(holdings[already])

    peer = FakePeer(holdings)
    fetched = fetch_delta(manifest, store, discover([peer]))

    assert already not in fetched
    assert already not in peer.fetched
    assert set(fetched) == set(manifest.chunks) - {already}
    dest = tmp_path / "out.gguf"
    reconstruct(manifest, store, dest)
    assert dest.read_bytes() == DATA


def test_tampered_chunk_is_rejected(tmp_path: Path) -> None:
    manifest, holdings = _fixture(tmp_path)
    store = ChunkStore(max_bytes=100_000)
    victim = manifest.chunks[0]
    peer = FakePeer(holdings, tamper={victim: b"not the real bytes"})

    with pytest.raises(VerificationError):
        fetch_delta(manifest, store, discover([peer]))
    assert not store.has(victim)


def test_missing_from_every_peer_raises(tmp_path: Path) -> None:
    manifest, holdings = _fixture(tmp_path)
    store = ChunkStore(max_bytes=100_000)
    dropped = manifest.chunks[-1]
    partial = {h: b for h, b in holdings.items() if h != dropped}

    with pytest.raises(ChunkNotFound):
        fetch_delta(manifest, store, discover([FakePeer(partial)]))


def test_resume_after_interruption_completes(tmp_path: Path) -> None:
    manifest, holdings = _fixture(tmp_path)
    store = ChunkStore(max_bytes=100_000)
    distinct = len(set(manifest.chunks))

    flaky = FakePeer(holdings, fail_after=2)
    with pytest.raises(RuntimeError):
        fetch_delta(manifest, store, discover([flaky]))
    got_first = list(store.availability())
    assert len(got_first) == 2

    healthy = FakePeer(holdings)
    fetch_delta(manifest, store, discover([healthy]))

    # The resume fetched only what was still missing, never a chunk already held.
    assert not set(healthy.fetched) & set(got_first)
    assert len(set(healthy.fetched)) == distinct - 2
    dest = tmp_path / "out.gguf"
    reconstruct(manifest, store, dest)
    assert dest.read_bytes() == DATA
