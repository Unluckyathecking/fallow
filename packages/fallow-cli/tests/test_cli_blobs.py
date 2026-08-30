"""Unit tests for local blob hashing and manifest construction."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from fallow_cli.blobs import build_manifest, dest_for, hash_file
from fallow_cli.errors import CliError
from fallow_protocol import WorkerKind


def test_hash_file_matches_hashlib(tmp_path: Path) -> None:
    blob = tmp_path / "weights.gguf"
    payload = b"fallow-model-bytes" * 4096
    blob.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()
    sha256, size = hash_file(blob)
    assert sha256 == expected
    assert size == len(payload)


def test_hash_file_empty_rejected(tmp_path: Path) -> None:
    blob = tmp_path / "empty.gguf"
    blob.write_bytes(b"")
    with pytest.raises(CliError) as exc:
        hash_file(blob)
    assert "empty" in exc.value.message


def test_hash_file_missing_rejected(tmp_path: Path) -> None:
    with pytest.raises(CliError) as exc:
        hash_file(tmp_path / "absent.gguf")
    assert "not found" in exc.value.message


def test_build_manifest_with_mmproj_hashes_companion(tmp_path: Path) -> None:
    blob = tmp_path / "vlm.gguf"
    blob.write_bytes(b"main-weights")
    mmproj = tmp_path / "mmproj.gguf"
    mmproj_bytes = b"projector-weights"
    mmproj.write_bytes(mmproj_bytes)

    manifest = build_manifest(
        path=blob,
        model_id="vlm-1",
        family="qwen2.5-vl",
        quant="Q4_K_M",
        worker_kind=WorkerKind.OCR,
        min_ram_mb=0,
        min_vram_mb=6500,
        mmproj_path=mmproj,
    )

    assert manifest.mmproj_file_name == "mmproj.gguf"
    assert manifest.mmproj_sha256 == hashlib.sha256(mmproj_bytes).hexdigest()
    assert manifest.mmproj_size_bytes == len(mmproj_bytes)


def test_build_manifest_mmproj_must_sit_beside_blob(tmp_path: Path) -> None:
    blob = tmp_path / "vlm.gguf"
    blob.write_bytes(b"main-weights")
    elsewhere = tmp_path / "sub"
    elsewhere.mkdir()
    mmproj = elsewhere / "mmproj.gguf"
    mmproj.write_bytes(b"projector-weights")

    with pytest.raises(CliError) as exc:
        build_manifest(
            path=blob,
            model_id="vlm-1",
            family="qwen2.5-vl",
            quant="Q4_K_M",
            worker_kind=WorkerKind.OCR,
            min_ram_mb=0,
            min_vram_mb=0,
            mmproj_path=mmproj,
        )
    assert "directory" in exc.value.message


def test_build_manifest_populates_fields(tmp_path: Path) -> None:
    blob = tmp_path / "m.gguf"
    blob.write_bytes(b"abc123")
    manifest = build_manifest(
        path=blob,
        model_id="m1",
        family="fam",
        quant="Q4_K_M",
        worker_kind=WorkerKind.EMBED,
        min_ram_mb=512,
        min_vram_mb=0,
        source_url="http://src/m.gguf",
    )
    assert manifest.file_name == "m.gguf"
    assert manifest.sha256 == hashlib.sha256(b"abc123").hexdigest()
    assert manifest.size_bytes == 6
    assert manifest.worker_kind is WorkerKind.EMBED
    assert manifest.source_url == "http://src/m.gguf"


def test_dest_for_uses_url_basename(tmp_path: Path) -> None:
    dest = dest_for("http://host/path/qwen.gguf", "qwen")
    assert dest.name == "qwen.gguf"


def test_dest_for_falls_back_to_model_id() -> None:
    dest = dest_for("http://host/", "qwen")
    assert dest.name == "qwen"
