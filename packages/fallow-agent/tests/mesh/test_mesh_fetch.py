"""The pure mesh fetch: manifest vetting, chunk verification, delta, reconstruct."""

from __future__ import annotations

from pathlib import Path

import pytest
from mesh_helpers import (
    CHUNK_SIZE,
    KEY,
    FakePeer,
    chunks_of,
    signed_payload,
    write_blob,
)

from fallow_agent.mesh.errors import MeshError
from fallow_agent.mesh.fetch import (
    fetch_and_reconstruct,
    seed_store_from_dir,
    verified_mesh_manifest,
)
from fallow_modelmesh import ChunkStore, VerificationError, build_manifest

_CAP = 1 << 20  # 1 MiB store, ample for the tiny test blobs.


def test_valid_manifest_is_accepted(tmp_path: Path) -> None:
    manifest = write_blob(tmp_path / "m.gguf", b"x" * 2500)
    parsed, signature = verified_mesh_manifest(
        signed_payload(manifest), KEY, manifest.whole_file_sha256
    )
    assert parsed.chunks == manifest.chunks
    assert signature == signed_payload(manifest)["signature"]


def test_tampered_signature_is_rejected(tmp_path: Path) -> None:
    manifest = write_blob(tmp_path / "m.gguf", b"x" * 2500)
    payload = signed_payload(manifest)
    payload["signature"] = "0" * len(payload["signature"])  # forged
    with pytest.raises(MeshError, match="signature does not verify"):
        verified_mesh_manifest(payload, KEY, manifest.whole_file_sha256)


def test_manifest_signed_with_wrong_key_is_rejected(tmp_path: Path) -> None:
    manifest = write_blob(tmp_path / "m.gguf", b"x" * 2500)
    payload = signed_payload(manifest, key=b"attacker-key")
    with pytest.raises(MeshError, match="signature does not verify"):
        verified_mesh_manifest(payload, KEY, manifest.whole_file_sha256)


def test_manifest_missing_signature_field_is_rejected(tmp_path: Path) -> None:
    manifest = write_blob(tmp_path / "m.gguf", b"x" * 2500)
    payload = signed_payload(manifest)
    del payload["signature"]
    with pytest.raises(MeshError):
        verified_mesh_manifest(payload, KEY, manifest.whole_file_sha256)


def test_sha_mismatch_is_rejected(tmp_path: Path) -> None:
    manifest = write_blob(tmp_path / "m.gguf", b"x" * 2500)
    with pytest.raises(MeshError, match="disagrees"):
        verified_mesh_manifest(signed_payload(manifest), KEY, "f" * 64)


def test_fetch_reconstructs_the_original_file(tmp_path: Path) -> None:
    data = bytes((i * 13) % 256 for i in range(2500))
    manifest = write_blob(tmp_path / "src.gguf", data)
    peer = FakePeer(chunks_of(data))
    dest = tmp_path / "out.gguf"

    fetch_and_reconstruct(
        manifest, signed_payload(manifest)["signature"], KEY, [peer], ChunkStore(_CAP), dest
    )

    assert dest.read_bytes() == data


def test_a_lying_peer_is_rejected(tmp_path: Path) -> None:
    data = b"y" * 2500
    manifest = write_blob(tmp_path / "src.gguf", data)
    peer = FakePeer(chunks_of(data), corrupt=True)
    dest = tmp_path / "out.gguf"

    with pytest.raises(VerificationError):
        fetch_and_reconstruct(
            manifest, signed_payload(manifest)["signature"], KEY, [peer], ChunkStore(_CAP), dest
        )
    assert not dest.exists()


def test_delta_upgrade_fetches_only_changed_chunks(tmp_path: Path) -> None:
    shared = bytes((i * 7) % 256 for i in range(CHUNK_SIZE))  # identical first chunk
    old = shared + b"A" * CHUNK_SIZE
    new = shared + b"B" * CHUNK_SIZE
    new_manifest = write_blob(tmp_path / "new.gguf", new)

    # Seed the store from the old blob still on disk, as a real upgrade would.
    old_dir = tmp_path / "cache"
    old_dir.mkdir()
    (old_dir / "old.gguf").write_bytes(old)
    store = ChunkStore(_CAP)
    seed_store_from_dir(store, old_dir, CHUNK_SIZE)

    peer = FakePeer(chunks_of(new))
    dest = tmp_path / "out.gguf"
    fetch_and_reconstruct(
        new_manifest, signed_payload(new_manifest)["signature"], KEY, [peer], store, dest
    )

    changed_hash = new_manifest.chunks[1]
    assert peer.fetched == [changed_hash]  # only the changed chunk crossed the wire
    assert dest.read_bytes() == new


def test_seed_skips_markers_and_partials(tmp_path: Path) -> None:
    (tmp_path / "blob.gguf").write_bytes(b"z" * 1500)
    (tmp_path / "blob.gguf.part").write_bytes(b"junk")
    (tmp_path / "blob.gguf.sha256").write_text("deadbeef")
    store = ChunkStore(_CAP)
    seed_store_from_dir(store, tmp_path, CHUNK_SIZE)

    manifest = build_manifest(tmp_path / "blob.gguf", "blob", CHUNK_SIZE)
    assert store.availability() == frozenset(manifest.chunks)  # only the blob seeded
