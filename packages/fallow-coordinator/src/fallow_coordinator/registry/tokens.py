"""Bearer-token machinery.

Every token (enrollment, device, api key) is a ``secrets.token_urlsafe`` string
handed to the client once and stored only as its sha256 hex digest. Verification
re-hashes the presented bearer and compares in constant time.
"""

import hashlib
import hmac
import secrets

from fallow_coordinator.registry.config import TOKEN_NBYTES

# An enrollment token's public name: the first 12 hex characters of its stored
# sha256. It is derivable from a join file the operator still holds and from
# nothing else, and it identifies a token without ever naming it (see
# docs/admin-api.md). The CLI recomputes the same prefix.
TOKEN_ID_CHARS = 12


def new_token(nbytes: int = TOKEN_NBYTES) -> str:
    """Mint a fresh URL-safe secret token."""
    return secrets.token_urlsafe(nbytes)


def token_id(token: str) -> str:
    """Return the public id an operator uses to name ``token``."""
    return hash_token(token)[:TOKEN_ID_CHARS]


def hash_token(token: str) -> str:
    """Return the sha256 hex digest stored at rest for ``token``."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_matches(presented: str, stored_hash: str) -> bool:
    """Constant-time check that ``presented`` hashes to ``stored_hash``."""
    return hmac.compare_digest(hash_token(presented), stored_hash)
