"""The on-disk layout of an offline seeding bundle.

A bundle is a plain directory a USB stick or file share can carry between an
online machine and an air-gapped one. It holds the signed manifest, its detached
signature, and one file per distinct chunk named by the chunk's content address:

    <bundle>/
        manifest.json      canonical manifest bytes (exactly what was signed)
        signature.txt      the detached hex signature over those bytes
        chunks/<hash>      one file per distinct chunk, keyed by its sha256

The manifest is written as its canonical bytes so the signature still verifies
byte-for-byte after the round trip. Parsing reads those same bytes back into a
``Manifest``; because the encoding is deterministic, re-serialising the parsed
value reproduces the signed bytes, so a tampered manifest.json fails the
signature check on import. This module owns only the format and the parse. The
export and import operations live in ``offline``.
"""

import json
from pathlib import Path

from fallow_modelmesh.errors import VerificationError
from fallow_modelmesh.manifest import Manifest

MANIFEST_NAME = "manifest.json"
SIGNATURE_NAME = "signature.txt"
CHUNKS_DIR = "chunks"

_STR_FIELDS = ("model_id", "whole_file_sha256", "merkle_root")
_INT_FIELDS = ("total_size", "chunk_size")


def chunk_path(bundle_dir: Path, chunk_hash: str) -> Path:
    """Return the path a chunk with ``chunk_hash`` occupies in the bundle."""
    return bundle_dir / CHUNKS_DIR / chunk_hash


def parse_manifest(data: bytes) -> Manifest:
    """Parse canonical manifest bytes read from a bundle into a ``Manifest``.

    The bytes come from an untrusted medium, so every field is checked for
    presence and type before a ``Manifest`` is built. A malformed file raises
    ``VerificationError`` rather than a bare ``KeyError`` or ``TypeError``. This
    only shapes the value; whether it is authentic is decided by the signature.
    """
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        raise VerificationError("manifest is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise VerificationError("manifest is not a JSON object")
    for field in _STR_FIELDS:
        if not isinstance(payload.get(field), str):
            raise VerificationError(f"manifest field {field!r} is missing or not a string")
    for field in _INT_FIELDS:
        # bool is an int subclass; reject it so a stray true/false is caught.
        value = payload.get(field)
        if not isinstance(value, int) or isinstance(value, bool):
            raise VerificationError(f"manifest field {field!r} is missing or not an integer")
    chunks = payload.get("chunks")
    if not isinstance(chunks, list) or not all(isinstance(h, str) for h in chunks):
        raise VerificationError("manifest field 'chunks' is missing or not a list of strings")
    return Manifest(
        model_id=payload["model_id"],
        total_size=payload["total_size"],
        chunk_size=payload["chunk_size"],
        whole_file_sha256=payload["whole_file_sha256"],
        chunks=tuple(chunks),
        merkle_root=payload["merkle_root"],
    )
