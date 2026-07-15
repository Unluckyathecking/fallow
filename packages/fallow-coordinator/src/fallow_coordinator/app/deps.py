"""Shared HTTP helpers for the coordinator routers (module I1).

Bearer extraction and the two auth dependencies (device-token for agents, admin
key for operators) live here so both route modules share one implementation and
one set of status-code conventions.
"""

from __future__ import annotations

from fastapi import HTTPException

from fallow_coordinator.app.state import CoordinatorState
from fallow_coordinator.registry import ApiKeyInfo


def extract_bearer(authorization: str | None) -> str | None:
    """Return the token from an ``Authorization: Bearer <token>`` header, or None."""
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


async def authenticate_agent(state: CoordinatorState, authorization: str | None) -> str:
    """Resolve a device token to an ``agent_id`` or raise 401."""
    token = extract_bearer(authorization)
    if token is None:
        raise HTTPException(status_code=401, detail="missing bearer token")
    agent_id = await state.registry.authenticate_agent(token)
    if agent_id is None:
        raise HTTPException(status_code=401, detail="invalid device token")
    return agent_id


async def authenticate_admin(state: CoordinatorState, authorization: str | None) -> ApiKeyInfo:
    """Resolve an admin key or raise 401 (unknown) / 403 (non-admin key)."""
    token = extract_bearer(authorization)
    if token is None:
        raise HTTPException(status_code=401, detail="missing admin key")
    info = await state.registry.authenticate_api_key(token)
    if info is None:
        raise HTTPException(status_code=401, detail="unknown admin key")
    if not info.is_admin:
        raise HTTPException(status_code=403, detail="admin scope required")
    return info
