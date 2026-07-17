"""Detached signing over a manifest.

The signature is HMAC-SHA256 over the manifest's canonical bytes, produced and
verified with the same repo pattern used for bearer tokens (``hmac`` plus a
constant-time compare). It is detached: the signature travels beside the
manifest, not inside it, so the signed bytes stay identical to what was hashed.

HMAC is symmetric. That fits the current trust model, where the coordinator is
the sole authority and hands the shared verification key to agents over the
already-authenticated enrolment channel (ADR 006). When manifests one day
originate from parties an agent cannot authenticate directly, this swaps for an
asymmetric signature (ed25519) without touching callers; see ADR 071.
"""

import hmac

from fallow_modelmesh.manifest import Manifest

_DIGEST = "sha256"


def sign_manifest(manifest: Manifest, key: bytes) -> str:
    """Return the detached hex signature over ``manifest`` under ``key``."""
    return hmac.new(key, manifest.canonical_bytes(), _DIGEST).hexdigest()


def verify_manifest(manifest: Manifest, signature: str, key: bytes) -> bool:
    """Report whether ``signature`` is a valid signature over ``manifest``.

    The compare is constant time. A tampered manifest changes the canonical
    bytes and fails; an empty or forged signature fails.
    """
    expected = sign_manifest(manifest, key)
    return hmac.compare_digest(expected, signature)
