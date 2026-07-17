from pathlib import Path

import pytest

from fallow_modelmesh.chunk import iter_file_chunks
from fallow_modelmesh.errors import ChunkNotFound, VerificationError
from fallow_modelmesh.manifest import Manifest, build_manifest
from fallow_modelmesh.safe import reconstruct_atomic, verified_reconstruct
from fallow_modelmesh.signing import sign_manifest
from fallow_modelmesh.store import ChunkStore

KEY = b"shared-signing-key"
DATA = b"gguf" + bytes(range(256)) * 8


def _full_store(manifest: Manifest, src: Path) -> ChunkStore:
    store = ChunkStore(max_bytes=100_000)
    for data in iter_file_chunks(src, manifest.chunk_size):
        store.put(data)
    return store


def _fixture(tmp_path: Path) -> tuple[Manifest, Path]:
    src = tmp_path / "model.gguf"
    src.write_bytes(DATA)
    return build_manifest(src, model_id="m1", chunk_size=64), src


def test_verified_reconstruct_round_trip(tmp_path: Path) -> None:
    manifest, src = _fixture(tmp_path)
    store = _full_store(manifest, src)
    dest = tmp_path / "out.gguf"
    verified_reconstruct(manifest, sign_manifest(manifest, KEY), KEY, store, dest)
    assert dest.read_bytes() == DATA


def test_unsigned_manifest_rejected_end_to_end(tmp_path: Path) -> None:
    manifest, src = _fixture(tmp_path)
    store = _full_store(manifest, src)
    dest = tmp_path / "out.gguf"
    with pytest.raises(VerificationError):
        verified_reconstruct(manifest, "", KEY, store, dest)
    assert not dest.exists()


def test_wrong_key_rejected_before_any_write(tmp_path: Path) -> None:
    manifest, src = _fixture(tmp_path)
    store = _full_store(manifest, src)
    dest = tmp_path / "out.gguf"
    signature = sign_manifest(manifest, KEY)
    with pytest.raises(VerificationError):
        verified_reconstruct(manifest, signature, b"a different key", store, dest)
    assert not dest.exists()


def test_failure_leaves_no_partial_file(tmp_path: Path) -> None:
    manifest, src = _fixture(tmp_path)
    # Hold every chunk except the last, so reconstruction writes some bytes and
    # then fails partway with the destination half-built.
    store = ChunkStore(max_bytes=100_000)
    chunks = list(iter_file_chunks(src, manifest.chunk_size))
    for data in chunks[:-1]:
        store.put(data)

    dest = tmp_path / "out.gguf"
    with pytest.raises(ChunkNotFound):
        reconstruct_atomic(manifest, store, dest)
    assert not dest.exists()
    assert not dest.with_name(dest.name + ".partial").exists()
