"""Bearer-token machinery.

Every token (enrollment, device, api key) is a ``secrets.token_urlsafe`` string
handed to the client once and stored only as its sha256 hex digest. Verification
re-hashes the presented bearer and compares in constant time.
"""

import hashlib
import hmac
import re
import secrets

from fallow_coordinator.registry.config import TOKEN_NBYTES
from fallow_coordinator.registry.errors import EnrollmentTokenError

# An enrollment token's public name: the first 12 hex characters of its stored
# sha256. It is derivable from a join file the operator still holds and from
# nothing else, and it identifies a token without ever naming it (see
# docs/admin-api.md). The CLI recomputes the same prefix.
TOKEN_ID_CHARS = 12


_TOKEN_ID_RE = re.compile(f"^[0-9a-f]{{{TOKEN_ID_CHARS}}}$")


def normalize_token_id(raw: str) -> str:
    """Canonicalise an operator-typed token id, or say plainly why it is not one.

    The id is a hex digest prefix, so case and surrounding whitespace carry no
    meaning and are removed. Anything else is a typo, and a typo that reached a
    ``substr`` comparison would silently match nothing and read as "already
    spent" — the one answer that would send an operator looking in the wrong
    place while a live join file is still out there.
    """
    candidate = raw.strip().lower()
    if not _TOKEN_ID_RE.match(candidate):
        raise EnrollmentTokenError(
            f"{raw!r} is not a token id: expected exactly {TOKEN_ID_CHARS} hex characters"
        )
    return candidate


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
