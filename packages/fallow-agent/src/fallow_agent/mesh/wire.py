"""Parse the coordinator's signed-manifest JSON into modelmesh types.

The coordinator sends ``{"manifest": {...six fields...}, "signature": "<hex>"}``
(see the coordinator's ``modelserve.mesh``). We rebuild the :class:`Manifest`
value and recompute the signature over its canonical bytes to verify, so only the
field values matter here, not the envelope shape. Anything malformed raises
:class:`MeshError` rather than a raw ``KeyError``/``TypeError`` so the caller can
treat a bad manifest as one mesh failure and fall back to the blob path.
"""

from __future__ import annotations

from typing import Any

from fallow_agent.mesh.errors import MeshError
from fallow_modelmesh import Manifest


def parse_signed_manifest(payload: Any) -> tuple[Manifest, str]:
    """Return ``(manifest, signature)`` from a decoded JSON payload."""
    if not isinstance(payload, dict):
        raise MeshError("signed manifest is not a JSON object")
    raw = payload.get("manifest")
    signature = payload.get("signature")
    if not isinstance(raw, dict) or not isinstance(signature, str):
        raise MeshError("signed manifest missing 'manifest' object or 'signature'")
    try:
        manifest = Manifest(
            model_id=str(raw["model_id"]),
            total_size=int(raw["total_size"]),
            chunk_size=int(raw["chunk_size"]),
            whole_file_sha256=str(raw["whole_file_sha256"]),
            chunks=tuple(str(chunk) for chunk in raw["chunks"]),
            merkle_root=str(raw["merkle_root"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MeshError(f"malformed manifest fields: {exc}") from exc
    return manifest, signature
