"""Agent-facing HTTP routes (module I1).

These are the exact paths and status codes the A5 ``CoordinatorClient`` dials
(see ``fallow_agent.heartbeat.constants``): register (201), heartbeat (200),
events (202), long-poll work (200-with-lease or 204), result (200), and the
unit-input fetch. Device-token auth (``registry.authenticate_agent``) guards
every route except registration.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Header, HTTPException, Request, Response

from fallow_coordinator.app.deps import authenticate_agent
from fallow_coordinator.app.result_blobs import ResultPayloadTooLarge
from fallow_coordinator.app.state import CoordinatorState
from fallow_coordinator.registry import (
    EnrollmentTokenError,
    ProtocolMismatchError,
    UnknownAgentError,
)
from fallow_coordinator.scheduler import select_for_poll
from fallow_protocol.messages import (
    AgentEvent,
    AgentSnapshot,
    AgentState,
    EventKind,
    Heartbeat,
    HeartbeatResponse,
    RegisterRequest,
    RegisterResponse,
    WorkResult,
)
from fallow_protocol.models import ReplicaState

_JSON = "application/json"
_OCTET = "application/octet-stream"


def build_agent_router(state: CoordinatorState) -> APIRouter:
    """Build the agent-facing router bound to ``state``."""
    router = APIRouter()

    async def require_agent(authorization: str | None = Header(default=None)) -> str:
        return await authenticate_agent(state, authorization)

    @router.post("/v1/agents/register", status_code=201)
    async def register(req: RegisterRequest, request: Request) -> RegisterResponse:
        host = request.client.host if request.client is not None else "unknown"
        try:
            return await state.registry.register_agent(req, host=host)
        except ProtocolMismatchError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except EnrollmentTokenError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    @router.post("/v1/agents/{agent_id}/heartbeat")
    async def heartbeat(agent_id: str, hb: Heartbeat, request: Request) -> HeartbeatResponse:
        await _authorize_self(state, agent_id, request)
        try:
            async with state.agent_liveness_lock:
                await state.registry.record_heartbeat(agent_id, hb)
        except UnknownAgentError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        desired = await state.registry.desired_models(agent_id)
        return HeartbeatResponse(desired_models=desired, revoked_lease_ids=(), config=None)

    @router.post("/v1/agents/{agent_id}/events", status_code=202)
    async def events(agent_id: str, event: AgentEvent, request: Request) -> Response:
        await _authorize_self(state, agent_id, request)
        await state.events.write(event)
        state.overrides.apply(event)
        # Push routing-visible state into the registry immediately so the
        # gateway also reacts now — never waits for the next heartbeat.
        if event.kind is EventKind.USER_RETURNED:
            async with state.agent_liveness_lock:
                await state.registry.set_agent_state(agent_id, AgentState.ACTIVE)
        elif event.kind is EventKind.USER_IDLE:
            async with state.agent_liveness_lock:
                await state.registry.set_agent_state(agent_id, AgentState.IDLE)
        return Response(status_code=202)

    @router.get("/v1/agents/{agent_id}/work")
    async def work(agent_id: str, request: Request, timeout: float = 0.0) -> Response:
        await _authorize_self(state, agent_id, request)
        return await _long_poll(state, agent_id, timeout)

    @router.post("/v1/agents/{agent_id}/work_units/{unit_id}/result", status_code=200)
    async def result(
        agent_id: str,
        unit_id: str,
        res: WorkResult,
        request: Request,
        x_fallow_lease_attempt: int = Header(alias="X-Fallow-Lease-Attempt", ge=1),
    ) -> Response:
        await _authorize_self(state, agent_id, request)
        if unit_id != res.work_unit_id:
            raise HTTPException(status_code=409, detail="result unit does not match request path")
        accepted = await state.queue.complete_unit(agent_id, x_fallow_lease_attempt, res)
        if not accepted:
            raise HTTPException(status_code=409, detail="work-unit result was not accepted")
        return Response(status_code=200)

    @router.post("/v1/agents/{agent_id}/work_units/{unit_id}/payload")
    async def payload(
        agent_id: str,
        unit_id: str,
        request: Request,
        x_fallow_lease_attempt: int = Header(alias="X-Fallow-Lease-Attempt", ge=1),
    ) -> dict[str, str]:
        await _authorize_self(state, agent_id, request)
        current_attempt = await state.queue.result_upload_attempt(agent_id, unit_id)
        if current_attempt != x_fallow_lease_attempt:
            raise HTTPException(status_code=409, detail="work-unit lease changed")
        try:
            digest = await state.results.put(request.stream())
        except ResultPayloadTooLarge as exc:
            raise HTTPException(status_code=413, detail=str(exc)) from exc
        accepted = await state.queue.bind_result_payload(
            agent_id, unit_id, x_fallow_lease_attempt, digest, digest
        )
        if not accepted:
            raise HTTPException(status_code=409, detail="work-unit lease changed during upload")
        return {"result_ref": digest}

    @router.get("/v1/work_units/{unit_id}/input")
    async def unit_input(unit_id: str, request: Request) -> Response:
        await require_agent(request.headers.get("authorization"))
        target = state.config.unit_input_dir / unit_id
        if not target.is_file():
            raise HTTPException(status_code=404, detail="unknown work-unit input")
        return Response(content=target.read_bytes(), media_type=_OCTET)

    return router


async def _authorize_self(state: CoordinatorState, agent_id: str, request: Request) -> None:
    """Authenticate the caller and require its token to match the path agent id."""
    caller = await authenticate_agent(state, request.headers.get("authorization"))
    if caller != agent_id:
        raise HTTPException(status_code=403, detail="device token does not match agent id")


async def _long_poll(state: CoordinatorState, agent_id: str, timeout: float) -> Response:
    """Long-poll for one leasable work unit until the deadline, else 204."""
    budget = min(max(timeout, 0.0), state.config.long_poll_max_s)
    deadline = state.now() + timedelta(seconds=budget)
    while True:
        lease_response = await _try_lease(state, agent_id)
        if lease_response is not None:
            return lease_response
        if state.now() >= deadline:
            return Response(status_code=204)
        await state.sleep(state.config.poll_sleep_s)


async def _try_lease(state: CoordinatorState, agent_id: str) -> Response | None:
    """One lease attempt: build the snapshot, gate it, and try the queue."""
    snapshot = await _agent_snapshot(state, agent_id)
    if snapshot is None:
        return None
    model_ids = tuple(r.model_id for r in snapshot.replicas if r.state == ReplicaState.READY)
    leasable = select_for_poll(snapshot, model_ids, state.policy)
    if not leasable:
        return None
    lease = await state.queue.lease_next(agent_id, leasable)
    if lease is None:
        return None
    return Response(content=lease.model_dump_json(), media_type=_JSON, status_code=200)


async def _agent_snapshot(state: CoordinatorState, agent_id: str) -> AgentSnapshot | None:
    """The agent's current routing snapshot, with any event-state override applied."""
    snapshots = await state.registry.snapshots(state.now())
    snapshot = next((s for s in snapshots if s.agent_id == agent_id), None)
    if snapshot is None:
        return None
    override = state.overrides.state_for(agent_id)
    if override is not None and override != snapshot.state:
        return snapshot.model_copy(update={"state": override})
    return snapshot
