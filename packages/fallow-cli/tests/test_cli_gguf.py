"""GGUF header parsing against hand-built fixture files.

Every fixture is assembled byte by byte in ``cli_helpers``, so the suite needs no
multi-GB weights and never touches the network.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest
from cli_helpers import (
    gguf_file,
    gguf_kv_string,
    gguf_kv_string_array,
    gguf_string,
    sample_gguf,
)

from fallow_cli.gguf import GgufError, derive_min_ram_mb, read_header


def _model_file(tmp_path: Path, payload: bytes, name: str = "m.gguf") -> Path:
    blob = tmp_path / name
    blob.write_bytes(payload)
    return blob


def test_reads_v3_header(tmp_path: Path) -> None:
    header = read_header(_model_file(tmp_path, sample_gguf() + b"\x00" * 64))
    assert header.version == 3
    assert header.tensor_count == 290
    assert header.architecture == "qwen2"
    assert header.name == "Qwen2.5 0.5B Instruct"
    assert header.quant == "Q4_K_M"


def test_reads_v2_header(tmp_path: Path) -> None:
    header = read_header(_model_file(tmp_path, sample_gguf(version=2, file_type=18)))
    assert header.version == 2
    assert header.quant == "Q6_K"


def test_unknown_file_type_derives_no_quant(tmp_path: Path) -> None:
    header = read_header(_model_file(tmp_path, sample_gguf(file_type=9999)))
    assert header.file_type == 9999
    assert header.quant is None


def test_missing_file_type_derives_no_quant(tmp_path: Path) -> None:
    payload = gguf_file([gguf_kv_string("general.architecture", "qwen2")])
    assert read_header(_model_file(tmp_path, payload)).quant is None


def test_wrong_magic_rejected(tmp_path: Path) -> None:
    blob = _model_file(tmp_path, b"NOTGGUF" + b"\x00" * 128, name="weights.bin")
    with pytest.raises(GgufError, match="not a GGUF file"):
        read_header(blob)


def test_v1_rejected_by_name(tmp_path: Path) -> None:
    blob = _model_file(tmp_path, b"GGUF" + struct.pack("<III", 1, 2, 0))
    with pytest.raises(GgufError, match="unsupported GGUF version 1"):
        read_header(blob)


def test_truncated_mid_string_rejected(tmp_path: Path) -> None:
    with pytest.raises(GgufError, match="truncated"):
        read_header(_model_file(tmp_path, sample_gguf()[:40]))


def test_truncated_inside_skipped_array_rejected(tmp_path: Path) -> None:
    """A short read hiding behind a seek must still fail, not silently succeed."""
    payload = gguf_file(
        [
            gguf_kv_string("general.architecture", "qwen2"),
            gguf_kv_string_array("tokenizer.ggml.tokens", ["aaaaaaaaaaaaaaaa"]),
        ]
    )
    with pytest.raises(GgufError, match="truncated"):
        read_header(_model_file(tmp_path, payload[:-8]))


def test_empty_file_rejected(tmp_path: Path) -> None:
    with pytest.raises(GgufError, match="truncated"):
        read_header(_model_file(tmp_path, b""))


def test_implausible_kv_count_rejected(tmp_path: Path) -> None:
    blob = _model_file(tmp_path, b"GGUF" + struct.pack("<IQQ", 3, 1, 1 << 40))
    with pytest.raises(GgufError, match="implausible"):
        read_header(blob)


def test_unknown_value_type_rejected(tmp_path: Path) -> None:
    payload = gguf_file([gguf_string("general.name") + struct.pack("<I", 99)])
    with pytest.raises(GgufError, match="unknown GGUF value type 99"):
        read_header(_model_file(tmp_path, payload))


def test_missing_file_is_a_gguf_error(tmp_path: Path) -> None:
    with pytest.raises(GgufError, match="could not read"):
        read_header(tmp_path / "absent.gguf")


def test_min_ram_rule_is_file_plus_headroom_plus_overhead() -> None:
    mib = 1024 * 1024
    assert derive_min_ram_mb(100 * mib) == 115 + 512
    # The real 0.5B Q4_K_M blob: 491_400_032 bytes, the first catalog entry.
    assert derive_min_ram_mb(491_400_032) == 1051
    assert derive_min_ram_mb(0) == 512
