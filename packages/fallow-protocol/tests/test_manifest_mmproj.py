"""OCR worker kind and the manifest's optional mmproj companion blob."""

import json

import pytest

from fallow_protocol.capabilities import WorkerKind
from fallow_protocol.models import ModelManifest

_SHA = "a" * 64


def _manifest_kwargs() -> dict:
    return {
        "model_id": "vlm-1",
        "family": "qwen2.5-vl",
        "quant": "Q4_K_M",
        "worker_kind": WorkerKind.OCR,
        "file_name": "vlm-1.gguf",
        "sha256": _SHA,
        "size_bytes": 100,
    }


def test_ocr_is_a_worker_kind() -> None:
    assert WorkerKind("ocr") is WorkerKind.OCR


def test_manifest_accepts_mmproj_triple() -> None:
    manifest = ModelManifest(
        **_manifest_kwargs(),
        mmproj_file_name="mmproj.gguf",
        mmproj_sha256=_SHA,
        mmproj_size_bytes=50,
    )
    assert manifest.mmproj_file_name == "mmproj.gguf"
    assert manifest.mmproj_sha256 == _SHA
    assert manifest.mmproj_size_bytes == 50


def test_manifest_rejects_partial_mmproj_fields() -> None:
    with pytest.raises(ValueError):
        ModelManifest(**_manifest_kwargs(), mmproj_file_name="mmproj.gguf")


def test_pre_mmproj_manifest_json_still_validates() -> None:
    # A manifest serialized before the mmproj fields existed must stay valid.
    old = dict(_manifest_kwargs())
    old["worker_kind"] = "chat"
    manifest = ModelManifest.model_validate(json.loads(json.dumps(old)))
    assert manifest.mmproj_file_name is None
    assert manifest.mmproj_sha256 is None
    assert manifest.mmproj_size_bytes is None
