from dataclasses import replace
from pathlib import Path

from fallow_modelmesh.manifest import Manifest, build_manifest
from fallow_modelmesh.signing import sign_manifest, verify_manifest

KEY = b"coordinator-signing-key"


def _manifest(tmp_path: Path) -> Manifest:
    path = tmp_path / "model.gguf"
    path.write_bytes(b"z" * 30)
    return build_manifest(path, model_id="m1", chunk_size=10)


def test_sign_then_verify(tmp_path: Path) -> None:
    m = _manifest(tmp_path)
    sig = sign_manifest(m, KEY)
    assert verify_manifest(m, sig, KEY) is True


def test_tampered_manifest_fails(tmp_path: Path) -> None:
    m = _manifest(tmp_path)
    sig = sign_manifest(m, KEY)
    forged = replace(m, model_id="m2")
    assert verify_manifest(forged, sig, KEY) is False


def test_unsigned_manifest_fails(tmp_path: Path) -> None:
    m = _manifest(tmp_path)
    assert verify_manifest(m, "", KEY) is False


def test_wrong_key_fails(tmp_path: Path) -> None:
    m = _manifest(tmp_path)
    sig = sign_manifest(m, KEY)
    assert verify_manifest(m, sig, b"other-key") is False
