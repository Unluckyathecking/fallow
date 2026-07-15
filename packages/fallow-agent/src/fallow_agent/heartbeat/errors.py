"""Typed errors raised by the coordinator client.

The hierarchy exists so callers can react by *class* of failure without
string-matching:

- :class:`CoordinatorAuthError` — the coordinator rejected our identity
  (401/403). Retrying with the same token is pointless; the heartbeat loop
  surfaces this and stops.
- :class:`CoordinatorTransientError` — a connection-level failure (DNS,
  connect, read timeout, reset) or a 5xx server response. Safe to retry later;
  idempotent calls retry it in-line, and the heartbeat loop keeps looping.
- :class:`CoordinatorProtocolError` — a well-formed HTTP exchange that violated
  the contract (unexpected status, malformed body, missing device token).
  Deterministic: retrying the same request will fail the same way.
"""


class CoordinatorError(Exception):
    """Base class for every coordinator-client failure."""


class CoordinatorAuthError(CoordinatorError):
    """Authentication/authorization was rejected (401/403)."""


class CoordinatorTransientError(CoordinatorError):
    """A retryable transport failure or 5xx server response."""


class CoordinatorProtocolError(CoordinatorError):
    """A non-retryable contract violation (bad status, malformed body)."""
