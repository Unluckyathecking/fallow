import json
from pathlib import Path

import pytest

from fallow_modelmesh.bundle import CHUNKS_DIR, MANIFEST_NAME, SIGNATURE_NAME, chunk_path
from fallow_modelmesh.chunk import iter_file_chunks
from fallow_modelmesh.errors import ChunkNotFound, VerificationError
from fallow_modelmesh.manifest import Manifest, build_manifest
from fallow_modelmesh.offline import export_bundle, import_bundle
from fallow_modelmesh.safe import verified_reconstruct
from fallow_modelmesh.signing import sign_manifest
from fallow_modelmesh.store import ChunkStore

KEY = b"shared-signing-key"
# Two 64-byte blocks laid out on chunk boundaries and repeated, plus a short
# tail. With chunk_size 64 this makes distinct chunks recur, so dedup on export
# and the delta-set on import both get exercised, and the tail is a partial chunk.
_A = bytes(range(64))
_B = bytes(range(64, 128))
DATA = _A + _B + _A + _B + _A + b"tail-bytes"


def _full_store(manifest: Manifest, src: Path) -> ChunkStore:
    store = ChunkStore(max_bytes=100_000)
    for data in iter_file_chunks(src, manifest.chunk_size):
        store.put(data)
    return store


def _fixture(tmp_path: Path) -> tuple[Manifest, str, Path]:
    src = tmp_path / "model.gguf"
    src.write_bytes(DATA)
    manifest = build_manifest(src, model_id="m1", chunk_size=64)
    return manifest, sign_manifest(manifest, KEY), src


def _exported(tmp_path: Path) -> tuple[Manifest, Path]:
    manifest, signature, src = _fixture(tmp_path)
    out = tmp_path / "bundle"
    export_bundle(manifest, signature, _full_store(manifest, src), out)
    return manifest, out


def test_export_import_round_trip_reconstructs_offline(tmp_path: Path) -> None:
    manifest, out = _exported(tmp_path)

    fresh = ChunkStore(max_bytes=100_000)
    imported = import_bundle(out, KEY, fresh)
    assert imported == manifest

    dest = tmp_path / "restored.gguf"
    verified_reconstruct(imported, sign_manifest(imported, KEY), KEY, fresh, dest)
    assert dest.read_bytes() == DATA


def test_tampered_chunk_is_rejected(tmp_path: Path) -> None:
    manifest, out = _exported(tmp_path)
    victim = manifest.chunks[0]
    chunk_path(out, victim).write_bytes(b"corrupted bytes")

    fresh = ChunkStore(max_bytes=100_000)
    with pytest.raises(VerificationError):
        import_bundle(out, KEY, fresh)


def test_tampered_manifest_is_rejected(tmp_path: Path) -> None:
    _, out = _exported(tmp_path)
    payload = json.loads((out / MANIFEST_NAME).read_bytes())
    payload["model_id"] = "swapped-in-model"
    (out / MANIFEST_NAME).write_bytes(json.dumps(payload).encode("utf-8"))

    fresh = ChunkStore(max_bytes=100_000)
    with pytest.raises(VerificationError):
        import_bundle(out, KEY, fresh)


def test_unsigned_manifest_is_rejected(tmp_path: Path) -> None:
    _, out = _exported(tmp_path)
    (out / SIGNATURE_NAME).write_text("", encoding="utf-8")

    fresh = ChunkStore(max_bytes=100_000)
    with pytest.raises(VerificationError):
        import_bundle(out, KEY, fresh)


def test_bad_signature_ingests_nothing(tmp_path: Path) -> None:
    _, out = _exported(tmp_path)
    (out / SIGNATURE_NAME).write_text("deadbeef", encoding="utf-8")

    fresh = ChunkStore(max_bytes=100_000)
    with pytest.raises(VerificationError):
        import_bundle(out, KEY, fresh)
    assert fresh.availability() == frozenset()


def test_partial_import_skips_present_chunks(tmp_path: Path) -> None:
    manifest, out = _exported(tmp_path)

    # Pre-load the store with every chunk but the last, then delete those chunk
    # files from the bundle. Import must skip the present ones (not read the
    # missing files) and pull only the one chunk the store still lacks.
    store = ChunkStore(max_bytes=100_000)
    present = manifest.chunks[:-1]
    last = manifest.chunks[-1]
    for h in dict.fromkeys(present):
        if h == last:
            continue
        store.put(chunk_path(out, h).read_bytes())
    for h in dict.fromkeys(present):
        if h != last:
            chunk_path(out, h).unlink()

    import_bundle(out, KEY, store)
    assert store.has(last)


def test_missing_required_chunk_raises(tmp_path: Path) -> None:
    manifest, out = _exported(tmp_path)
    for h in dict.fromkeys(manifest.chunks):
        chunk_path(out, h).unlink()

    fresh = ChunkStore(max_bytes=100_000)
    with pytest.raises(ChunkNotFound):
        import_bundle(out, KEY, fresh)


def test_export_writes_one_file_per_distinct_chunk(tmp_path: Path) -> None:
    manifest, out = _exported(tmp_path)
    written = {p.name for p in (out / CHUNKS_DIR).iterdir()}
    assert written == set(manifest.chunks)
    assert len(manifest.chunks) > len(written)  # the fixture repeats a chunk


def test_malformed_manifest_json_is_rejected(tmp_path: Path) -> None:
    _, out = _exported(tmp_path)
    (out / MANIFEST_NAME).write_bytes(b"{not json")

    fresh = ChunkStore(max_bytes=100_000)
    with pytest.raises(VerificationError):
        import_bundle(out, KEY, fresh)
