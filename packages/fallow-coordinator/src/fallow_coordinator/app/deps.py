"""Shared HTTP helpers for the coordinator routers (module I1).

The two auth dependencies (device-token for agents, admin key for operators)
live here so both route modules share one implementation and one set of
status-code conventions. Bearer extraction and device-token auth themselves
live in :mod:`fallow_coordinator.httpauth`, shared with ``modelserve``.
"""

from __future__ import annotations

from fastapi import HTTPException

from fallow_coordinator.app.state import CoordinatorState
from fallow_coordinator.httpauth import authenticate_agent as _authenticate_agent
from fallow_coordinator.httpauth import extract_bearer
from fallow_coordinator.registry import ApiKeyInfo

__all__ = ["authenticate_admin", "authenticate_agent", "extract_bearer"]


async def authenticate_agent(state: CoordinatorState, authorization: str | None) -> str:
    """Resolve a device token to an ``agent_id`` or raise 401."""
    return await _authenticate_agent(state.registry, authorization)


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
