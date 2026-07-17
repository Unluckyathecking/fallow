from pathlib import Path

from fallow_modelmesh.chunk import iter_file_chunks
from fallow_modelmesh.delta import missing_chunks
from fallow_modelmesh.manifest import Manifest, build_manifest
from fallow_modelmesh.store import ChunkStore


def _manifest_and_chunks(path: Path, data: bytes) -> tuple[Manifest, list[bytes]]:
    path.write_bytes(data)
    manifest = build_manifest(path, model_id="m", chunk_size=10)
    return manifest, list(iter_file_chunks(path, chunk_size=10))


def test_empty_store_needs_every_distinct_chunk(tmp_path: Path) -> None:
    manifest, _ = _manifest_and_chunks(tmp_path / "v1.gguf", b"a" * 10 + b"b" * 10)
    store = ChunkStore(max_bytes=10_000)
    assert missing_chunks(manifest, store) == manifest.chunks


def test_delta_excludes_held_chunks(tmp_path: Path) -> None:
    manifest, chunks = _manifest_and_chunks(tmp_path / "v1.gguf", b"a" * 10 + b"b" * 10)
    store = ChunkStore(max_bytes=10_000)
    store.put(chunks[0])
    assert missing_chunks(manifest, store) == (manifest.chunks[1],)


def test_repeated_chunk_listed_once(tmp_path: Path) -> None:
    manifest, _ = _manifest_and_chunks(tmp_path / "v1.gguf", b"a" * 10 + b"a" * 10)
    store = ChunkStore(max_bytes=10_000)
    assert len(missing_chunks(manifest, store)) == 1


def test_dedup_across_two_manifests_sharing_chunks(tmp_path: Path) -> None:
    # v2 keeps v1's first chunk and replaces the second: content addressing
    # means the shared chunk is identical, so a store holding v1 only needs the
    # one new chunk for v2.
    v1, v1_chunks = _manifest_and_chunks(tmp_path / "v1.gguf", b"a" * 10 + b"b" * 10)
    v2, _ = _manifest_and_chunks(tmp_path / "v2.gguf", b"a" * 10 + b"c" * 10)
    assert v1.chunks[0] == v2.chunks[0]

    store = ChunkStore(max_bytes=10_000)
    for data in v1_chunks:
        store.put(data)
    assert missing_chunks(v2, store) == (v2.chunks[1],)
