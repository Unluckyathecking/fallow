import hashlib
from pathlib import Path

from fallow_modelmesh.manifest import build_manifest
from fallow_modelmesh.merkle import merkle_root


def _write(tmp_path: Path, data: bytes) -> Path:
    path = tmp_path / "model.gguf"
    path.write_bytes(data)
    return path


def test_manifest_fields(tmp_path: Path) -> None:
    data = b"x" * 25
    path = _write(tmp_path, data)
    m = build_manifest(path, model_id="m1", chunk_size=10)
    assert m.model_id == "m1"
    assert m.total_size == 25
    assert m.chunk_size == 10
    assert m.whole_file_sha256 == hashlib.sha256(data).hexdigest()
    assert len(m.chunks) == 3
    assert m.merkle_root == merkle_root(m.chunks)


def test_repeated_chunks_share_a_hash(tmp_path: Path) -> None:
    path = _write(tmp_path, b"aaaaaaaaaa" + b"aaaaaaaaaa")
    m = build_manifest(path, model_id="m1", chunk_size=10)
    assert m.chunks[0] == m.chunks[1]


def test_canonical_bytes_are_deterministic(tmp_path: Path) -> None:
    path = _write(tmp_path, b"y" * 30)
    a = build_manifest(path, model_id="m1", chunk_size=10)
    b = build_manifest(path, model_id="m1", chunk_size=10)
    assert a.canonical_bytes() == b.canonical_bytes()


def test_empty_file_has_defined_manifest(tmp_path: Path) -> None:
    path = _write(tmp_path, b"")
    m = build_manifest(path, model_id="m1", chunk_size=10)
    assert m.total_size == 0
    assert m.chunks == ()
    assert m.merkle_root == merkle_root(())
