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
