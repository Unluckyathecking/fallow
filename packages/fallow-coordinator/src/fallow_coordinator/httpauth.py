"""Shared HTTP auth helpers used across coordinator layers.

Bearer-token extraction and device-token agent authentication were previously
duplicated in ``app/deps.py`` and ``modelserve/router.py``. Both now delegate
here. This module has no layer of its own in the "coordinator internal
layers" import-linter contract, so it can be imported by ``app``,
``modelserve``, and (once migrated) ``gateway`` alike without creating a
layering violation.
"""

from __future__ import annotations

from typing import Protocol

from fastapi import HTTPException

from fallow_coordinator.registry.errors import RevokedAgentError

# The one machine-readable 401 detail on the agent-facing path. The Go agent
# compares the ``detail`` field byte for byte to tell "an operator revoked this
# identity" — terminal, write the revocation marker and stay down — from every
# other rejection, which it retries. Changing this string changes agent
# behaviour; the Go constant is heartbeat.revokedDetail (ADR 104).
REVOKED_DEVICE_TOKEN_DETAIL = "device token revoked"


def extract_bearer(authorization: str | None) -> str | None:
    """Return the token from an ``Authorization: Bearer <token>`` header, or None."""
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


class AgentRegistry(Protocol):
    """The narrow registry surface device-token auth depends on."""

    async def authenticate_agent(self, bearer: str) -> str | None: ...


async def authenticate_agent(registry: AgentRegistry, authorization: str | None) -> str:
    """Resolve a device token to an ``agent_id`` or raise 401.

    Three details, and they are not interchangeable: no header at all, a token
    this coordinator does not recognise, and a token whose agent was revoked.
    Only the last is terminal, and only the last carries
    :data:`REVOKED_DEVICE_TOKEN_DETAIL`.
    """
    token = extract_bearer(authorization)
    if token is None:
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        agent_id = await registry.authenticate_agent(token)
    except RevokedAgentError as exc:
        raise HTTPException(status_code=401, detail=REVOKED_DEVICE_TOKEN_DETAIL) from exc
    if agent_id is None:
        raise HTTPException(status_code=401, detail="invalid device token")
    return agent_id
