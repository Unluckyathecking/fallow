"""Admin HTTP routes (`/v1/admin/*`, module I1).

Implements the eight routes in ``docs/admin-api.md`` exactly, against which the
``flw`` CLI (module L1) is already built and tested: enrollment tokens, api keys,
agent listing, model list/register, assignment replace, and job submit/status.
Every route requires ``Authorization: Bearer <admin key>`` (401 unknown / 403
non-admin), and error bodies use FastAPI's ``{"detail": ...}`` envelope.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request, Response

from fallow_coordinator.app.admin_models import (
    ApiKeyRequest,
    AssignmentRequest,
    ModelRegisterRequest,
)
from fallow_coordinator.app.chunker import ChunkError, chunk_job
from fallow_coordinator.app.deps import authenticate_admin
from fallow_coordinator.app.state import CoordinatorState
from fallow_protocol.messages import AgentSnapshot, JobStatus, JobSubmit
from fallow_protocol.models import ModelManifest


def build_admin_router(state: CoordinatorState) -> APIRouter:
    """Build the admin router (prefixed ``/v1/admin``) bound to ``state``."""
    router = APIRouter(prefix="/v1/admin")

    async def require_admin(authorization: str | None = Header(default=None)) -> None:
        await authenticate_admin(state, authorization)

    @router.post("/enrollment_tokens", status_code=201)
    async def create_enrollment_token(request: Request) -> dict[str, str]:
        await require_admin(request.headers.get("authorization"))
        token = await state.registry.create_enrollment_token()
        return {"token": token}

    @router.post("/api_keys", status_code=201)
    async def create_api_key(body: ApiKeyRequest, request: Request) -> dict[str, str]:
        await require_admin(request.headers.get("authorization"))
        allowlist = None if body.model_allowlist is None else list(body.model_allowlist)
        key = await state.registry.create_api_key(body.name, allowlist)
        return {"key": key}

    @router.get("/agents")
    async def list_agents(request: Request) -> list[AgentSnapshot]:
        await require_admin(request.headers.get("authorization"))
        return list(await state.registry.snapshots(state.now()))

    @router.get("/models")
    async def list_models(request: Request) -> list[ModelManifest]:
        await require_admin(request.headers.get("authorization"))
        return list(await state.registry.list_models())

    @router.post("/models", status_code=201)
    async def register_model(body: ModelRegisterRequest, request: Request) -> Response:
        await require_admin(request.headers.get("authorization"))
        if not Path(body.blob_path).is_file():
            raise HTTPException(status_code=422, detail=f"blob_path not found: {body.blob_path}")
        # sha256/size are trusted from the manifest; hashing a multi-GB blob on the
        # request path is too slow (documented in ADR 014).
        await state.registry.put_model(body.manifest, body.blob_path)
        return Response(status_code=201)

    @router.put("/assignments", status_code=204)
    async def set_assignments(body: AssignmentRequest, request: Request) -> Response:
        await require_admin(request.headers.get("authorization"))
        await _replace_model_assignment(state, body.model_id, body.agent_ids)
        return Response(status_code=204)

    @router.post("/jobs", status_code=201)
    async def submit_job(job: JobSubmit, request: Request) -> JobStatus:
        await require_admin(request.headers.get("authorization"))
        try:
            units = chunk_job(job, state.config.unit_input_dir, state.config.chunks_per_unit)
        except ChunkError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        job_id = await state.queue.submit_job(job, units)
        status = await state.queue.job_status(job_id)
        if status is None:  # pragma: no cover - just-submitted job always exists
            raise HTTPException(status_code=500, detail="job vanished after submit")
        return status

    @router.get("/jobs/{job_id}")
    async def job_status(job_id: str, request: Request) -> JobStatus:
        await require_admin(request.headers.get("authorization"))
        status = await state.queue.job_status(job_id)
        if status is None:
            raise HTTPException(status_code=404, detail=f"unknown job: {job_id}")
        return status

    return router


async def _replace_model_assignment(
    state: CoordinatorState, model_id: str, agent_ids: tuple[str, ...]
) -> None:
    """Idempotent replace: exactly ``agent_ids`` serve ``model_id`` afterwards.

    The registry only exposes a per-agent assignment setter, so this recomputes
    each affected agent's model set: target agents gain ``model_id``; any other
    agent that currently holds it loses it. The full agent set is the union of
    online snapshots, offline agents, and the requested targets.
    """
    now = state.now()
    online = {s.agent_id for s in await state.registry.snapshots(now)}
    offline = set(await state.registry.list_offline(now))
    targets = set(agent_ids)
    for agent_id in online | offline | targets:
        current = set(await state.registry.desired_models(agent_id))
        updated = current | {model_id} if agent_id in targets else current - {model_id}
        if updated != current:
            await state.registry.set_assignments(agent_id, sorted(updated))
