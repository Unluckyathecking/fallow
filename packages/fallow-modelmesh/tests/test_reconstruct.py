from pathlib import Path

import pytest

from fallow_modelmesh.chunk import iter_file_chunks
from fallow_modelmesh.errors import ChunkNotFound, VerificationError
from fallow_modelmesh.manifest import Manifest, build_manifest
from fallow_modelmesh.reconstruct import reconstruct
from fallow_modelmesh.store import ChunkStore

DATA = b"gguf-header" + bytes(range(256)) * 40


def _load(tmp_path: Path) -> tuple[Manifest, ChunkStore]:
    src = tmp_path / "model.gguf"
    src.write_bytes(DATA)
    manifest = build_manifest(src, model_id="m1", chunk_size=64)
    store = ChunkStore(max_bytes=10_000)
    for data in iter_file_chunks(src, chunk_size=64):
        store.put(data)
    return manifest, store


def test_round_trip(tmp_path: Path) -> None:
    manifest, store = _load(tmp_path)
    dest = tmp_path / "out.gguf"
    reconstruct(manifest, store, dest)
    assert dest.read_bytes() == DATA


def test_missing_chunk_rejected(tmp_path: Path) -> None:
    manifest, _ = _load(tmp_path)
    empty = ChunkStore(max_bytes=10_000)
    with pytest.raises(ChunkNotFound):
        reconstruct(manifest, empty, tmp_path / "out.gguf")


def test_tampered_chunk_rejected(tmp_path: Path) -> None:
    manifest, store = _load(tmp_path)
    # Corrupt the bytes held under a real chunk key, as a rotted disk or a
    # dishonest peer would, and confirm reconstruction refuses them.
    victim = manifest.chunks[0]
    store._chunks[victim] = b"corrupted"
    with pytest.raises(VerificationError):
        reconstruct(manifest, store, tmp_path / "out.gguf")
