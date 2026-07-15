"""Derive bounded, opaque affinity keys from request context."""

from hashlib import sha256

from fallow_coordinator.gateway.bodyparse import ParsedBody

_MESSAGE_PREFIX_CHARS = 256


def derive_session_key(
    model_id: str,
    explicit_session: str | None,
    bearer: str,
    parsed: ParsedBody,
) -> str | None:
    """Prefer the session header, then hash the key and first user message."""
    if explicit_session is not None and explicit_session.strip():
        source = "header\0" + explicit_session.strip()
    elif parsed.first_user_message is not None:
        prefix = parsed.first_user_message[:_MESSAGE_PREFIX_CHARS]
        source = "fallback\0" + bearer + "\0" + prefix
    else:
        return None
    digest = sha256(source.encode("utf-8")).hexdigest()
    return f"{model_id}:{digest}"
