"""Admin HTTP routes (`/v1/admin/*`, module I1).

Implements the eight routes in ``docs/admin-api.md`` exactly, against which the
``flw`` CLI (module L1) is already built and tested: enrollment tokens, api keys,
agent listing, model list/register, assignment replace, and job submit/status.
Every route requires ``Authorization: Bearer <admin key>`` (401 unknown / 403
non-admin), and error bodies use FastAPI's ``{"detail": ...}`` envelope.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse

from fallow_coordinator.app.admin_models import (
    ApiKeyRequest,
    AssignmentRequest,
    DocumentUploadRequest,
    ModelRegisterRequest,
)
from fallow_coordinator.app.chunker import ChunkError, chunk_job
from fallow_coordinator.app.deps import authenticate_admin
from fallow_coordinator.app.metrics import GetInflight, format_metrics, read_gateway_counters
from fallow_coordinator.app.rag_ingestion import (
    IngestionNotFoundError,
    IngestionPayloadError,
)
from fallow_coordinator.app.state import CoordinatorState
from fallow_coordinator.registry import (
    EnrollmentTokenError,
    EnrollmentTokenInfo,
    RevokedAgentInfo,
    Transport,
    UnknownAgentError,
)
from fallow_coordinator.scheduler import FitReport, model_fit
from fallow_protocol.messages import AgentSnapshot, JobStatus, JobSubmit
from fallow_protocol.models import ModelManifest

PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def build_metrics_router(state: CoordinatorState, get_inflight: GetInflight) -> APIRouter:
    """Build the admin-protected top-level metrics route."""
    router = APIRouter()

    @router.get("/metrics")
    async def metrics(request: Request) -> Response:
        await authenticate_admin(state, request.headers.get("authorization"))
        snapshots = await state.registry.snapshots(state.now())
        counters = await asyncio.to_thread(read_gateway_counters, state.config.gateway_log_path)
        return Response(
            content=format_metrics(snapshots, counters, get_inflight()),
            headers={"content-type": PROMETHEUS_CONTENT_TYPE},
        )

    return router


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

    @router.get("/enrollment_tokens")
    async def list_enrollment_tokens(request: Request) -> list[EnrollmentTokenInfo]:
        await require_admin(request.headers.get("authorization"))
        return list(await state.registry.list_enrollment_tokens())

    @router.delete("/enrollment_tokens/{token_id}", status_code=204)
    async def revoke_enrollment_token(token_id: str, request: Request) -> Response:
        await require_admin(request.headers.get("authorization"))
        # A malformed or ambiguous id is its own answer, and must not read as
        # "already spent" — that is the reading that leaves a live join file out
        # there while the operator believes it is dead.
        try:
            voided = await state.registry.revoke_enrollment_token(token_id)
        except EnrollmentTokenError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if not voided:
            raise HTTPException(
                status_code=404, detail=f"unknown or already spent enrollment token: {token_id}"
            )
        return Response(status_code=204)

    @router.post("/agents/{agent_id}/revoke", status_code=204)
    async def revoke_agent(agent_id: str, request: Request) -> Response:
        await require_admin(request.headers.get("authorization"))
        # Read past the revocation fence on purpose: revoke_agent is idempotent
        # and a retry after a partial failure (revoked_at committed, eviction
        # failed) must still see a relay agent here, or it would skip the very
        # eviction it is being retried for and 204 over surviving relay work.
        relayed = await _is_relay_agent(state, agent_id)
        try:
            await state.registry.revoke_agent(agent_id)
        except UnknownAgentError as exc:
            raise HTTPException(status_code=404, detail=f"unknown agent: {agent_id}") from exc
        # The agent is already out of every routing view; drop what it was
        # assigned so nothing desires a replica there, and cut its relay work.
        await state.registry.set_assignments(agent_id, [])
        if relayed:
            await _evict_from_relay(state, agent_id)
        return Response(status_code=204)

    @router.post("/api_keys", status_code=201)
    async def create_api_key(body: ApiKeyRequest, request: Request) -> dict[str, str]:
        await require_admin(request.headers.get("authorization"))
        allowlist = None if body.model_allowlist is None else list(body.model_allowlist)
        key = await state.registry.create_api_key(
            body.name, allowlist, body.rpm_limit, body.daily_limit
        )
        return {"key": key}

    @router.get("/agents")
    async def list_agents(request: Request) -> list[AgentSnapshot]:
        await require_admin(request.headers.get("authorization"))
        return list(await state.registry.snapshots(state.now()))

    @router.get("/agents/revoked")
    async def list_revoked_agents(request: Request) -> list[RevokedAgentInfo]:
        await require_admin(request.headers.get("authorization"))
        return list(await state.registry.list_revoked_agents())

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

    @router.get("/agents/{agent_id}/fit")
    async def agent_fit(agent_id: str, model_id: str, request: Request) -> dict[str, int | bool]:
        await require_admin(request.headers.get("authorization"))
        manifest = await state.registry.get_manifest(model_id)
        if manifest is None:
            raise HTTPException(status_code=404, detail=f"unknown model: {model_id}")
        agent = await _snapshot_for(state, agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail=f"unknown or offline agent: {agent_id}")
        report = model_fit(manifest, agent)
        return {
            "fits": report.fits,
            "required_vram_mb": report.required_vram_mb,
            "required_ram_mb": report.required_ram_mb,
            "available_vram_mb": report.available_vram_mb,
            "available_ram_mb": report.available_ram_mb,
        }

    @router.put("/assignments", status_code=204)
    async def set_assignments(body: AssignmentRequest, request: Request) -> Response:
        await require_admin(request.headers.get("authorization"))
        await _reject_unfit_assignment(state, body.model_id, body.agent_ids)
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

    @router.get("/work_units/{unit_id}/payload")
    async def work_unit_payload(unit_id: str, request: Request) -> FileResponse:
        await require_admin(request.headers.get("authorization"))
        result_ref = await state.queue.completed_result_ref(unit_id)
        if result_ref is None or not _is_sha256(result_ref):
            raise HTTPException(status_code=404, detail="work-unit payload not found")
        target = state.config.result_dir / result_ref
        if not target.is_file():
            raise HTTPException(status_code=404, detail="work-unit payload not found")
        return FileResponse(target, media_type="application/octet-stream")

    @router.post("/rag/collections/{collection}/documents", status_code=202)
    async def ingest_documents(
        collection: str, body: DocumentUploadRequest, request: Request
    ) -> dict[str, object]:
        await require_admin(request.headers.get("authorization"))
        if state.ingestion is None:
            raise HTTPException(status_code=503, detail="RAG vector store is not configured")
        try:
            job = await state.ingestion.submit(collection, body.model_id, body.chunks)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            ingestion = await state.ingestion.status(collection, job.job_id)
        except IngestionPayloadError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {
            "ingestion_id": job.job_id,
            "state": ingestion.state,
            "total_units": ingestion.total_units,
            "done_units": ingestion.done_units,
            "dead_units": ingestion.dead_units,
            "indexed_chunks": ingestion.indexed_chunks,
        }

    @router.get("/rag/collections/{collection}/ingestions/{ingestion_id}")
    async def ingestion_status(
        collection: str, ingestion_id: str, request: Request
    ) -> dict[str, object]:
        await require_admin(request.headers.get("authorization"))
        if state.ingestion is None:
            raise HTTPException(status_code=503, detail="RAG vector store is not configured")
        try:
            status = await state.ingestion.status(collection, ingestion_id)
        except IngestionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except IngestionPayloadError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {
            "ingestion_id": status.ingestion_id,
            "state": status.state,
            "total_units": status.total_units,
            "done_units": status.done_units,
            "dead_units": status.dead_units,
            "indexed_chunks": status.indexed_chunks,
        }

    return router


async def _is_relay_agent(state: CoordinatorState, agent_id: str) -> bool:
    """Whether this agent's work runs over the relay.

    A direct agent has no relay work, and its replicas already left
    ``replica_endpoints``, so there is nothing for the eviction below to do.

    Read from the registry, not through ``site_route``: that resolver is the
    external revocation fence and answers ``None`` for a revoked row forever,
    so a revocation retried after a partial failure — row marked, eviction
    failed — would read ``False`` here and skip the eviction it exists to
    finish.
    """
    if state.relay is None or state.site_route is None:
        return False
    return await state.registry.agent_transport(agent_id) is Transport.SITE_RELAY


async def _evict_from_relay(state: CoordinatorState, agent_id: str) -> None:
    """Drop a revoked site agent's queued and in-flight relay work at once.

    Same fence the presence path uses (ADR 081): persist a newer generation, then
    invalidate everything the broker still holds at an older one. This clears
    what already exists; ``site_route`` refusing the revoked row is what stops
    anything new forming, and the two together are the whole fence.
    """
    assert state.relay is not None
    generation = await state.registry.bump_presence_generation(agent_id)
    await state.relay.invalidate_agent(agent_id, generation, "revoked")


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


async def _snapshot_for(state: CoordinatorState, agent_id: str) -> AgentSnapshot | None:
    """The agent's current snapshot, or None when it has no live view."""
    for snapshot in await state.registry.snapshots(state.now()):
        if snapshot.agent_id == agent_id:
            return snapshot
    return None


async def _reject_unfit_assignment(
    state: CoordinatorState, model_id: str, agent_ids: tuple[str, ...]
) -> None:
    """Reject the whole assignment with 409 if any target cannot hold the model.

    The check runs before any write, so the endpoint stays all-or-nothing. Only
    agents with a live snapshot are checked: an unregistered model or an agent
    the coordinator has no current view of is left to the existing path.
    """
    manifest = await state.registry.get_manifest(model_id)
    if manifest is None:
        return
    targets = set(agent_ids)
    if not targets:
        return
    snapshots = {s.agent_id: s for s in await state.registry.snapshots(state.now())}
    for agent_id in sorted(targets):
        agent = snapshots.get(agent_id)
        if agent is None:
            continue
        # An agent already assigned this model has already paid its memory
        # footprint, so its reported free RAM/VRAM would wrongly fail the check.
        # Re-asserting an existing mapping must not start rejecting it.
        if model_id in await state.registry.desired_models(agent_id):
            continue
        report = model_fit(manifest, agent)
        if not report.fits:
            raise HTTPException(status_code=409, detail=_fit_rejection(model_id, agent_id, report))


def _fit_rejection(model_id: str, agent_id: str, report: FitReport) -> str:
    return (
        f"model {model_id!r} does not fit agent {agent_id!r}: "
        f"needs {report.required_ram_mb} MB RAM and {report.required_vram_mb} MB VRAM, "
        f"agent has {report.available_ram_mb} MB RAM and {report.available_vram_mb} MB VRAM"
    )


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
