import hashlib
from pathlib import Path

from fallow_modelmesh.chunk import chunk_hash, iter_file_chunks


def test_chunk_hash_is_sha256_hex() -> None:
    data = b"the quick brown fox"
    assert chunk_hash(data) == hashlib.sha256(data).hexdigest()


def test_iter_splits_on_chunk_size(tmp_path: Path) -> None:
    path = tmp_path / "blob.bin"
    path.write_bytes(b"a" * 25)
    chunks = list(iter_file_chunks(path, chunk_size=10))
    assert [len(c) for c in chunks] == [10, 10, 5]


def test_iter_empty_file_yields_nothing(tmp_path: Path) -> None:
    path = tmp_path / "empty.bin"
    path.write_bytes(b"")
    assert list(iter_file_chunks(path, chunk_size=10)) == []
