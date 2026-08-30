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


@pytest.mark.parametrize(
    "name",
    ["", ".", "..", "../projector.gguf", "sub/projector.gguf", "/abs/projector.gguf"],
)
def test_manifest_rejects_non_basename_mmproj_names(name: str) -> None:
    with pytest.raises(ValueError):
        ModelManifest(
            **_manifest_kwargs(),
            mmproj_file_name=name,
            mmproj_sha256=_SHA,
            mmproj_size_bytes=50,
        )


@pytest.mark.parametrize(
    "name",
    ["vlm-1.gguf", "vlm-1.gguf.part", "vlm-1.gguf.sha256"],
)
def test_manifest_rejects_mmproj_aliasing_blob_or_markers(name: str) -> None:
    # file_name is "vlm-1.gguf"; a companion named like the blob or its cache
    # markers would share a path with it and never verify (or feed the model
    # GGUF to llama-server as --mmproj).
    with pytest.raises(ValueError):
        ModelManifest(
            **_manifest_kwargs(),
            mmproj_file_name=name,
            mmproj_sha256="b" * 64,
            mmproj_size_bytes=50,
        )


def test_pre_mmproj_manifest_json_still_validates() -> None:
    # A manifest serialized before the mmproj fields existed must stay valid.
    old = dict(_manifest_kwargs())
    old["worker_kind"] = "chat"
    manifest = ModelManifest.model_validate(json.loads(json.dumps(old)))
    assert manifest.mmproj_file_name is None
    assert manifest.mmproj_sha256 is None
    assert manifest.mmproj_size_bytes is None
