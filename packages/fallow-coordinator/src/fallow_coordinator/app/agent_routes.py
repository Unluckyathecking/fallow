"""Agent-facing HTTP routes (module I1).

These are the exact paths and status codes the A5 ``CoordinatorClient`` dials
(see ``fallow_agent.heartbeat.constants``): register (201), heartbeat (200),
events (202), long-poll work (200-with-lease or 204), result (200), and the
unit-input fetch. Device-token auth (``registry.authenticate_agent``) guards
every route except registration.
"""

from __future__ import annotations

import base64
import contextlib
import logging
from collections.abc import Sequence
from datetime import timedelta

from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from fallow_coordinator.app.deps import authenticate_agent
from fallow_coordinator.app.result_blobs import ResultPayloadTooLarge
from fallow_coordinator.app.state import CoordinatorState
from fallow_coordinator.registry import (
    EnrollmentTokenError,
    ProtocolMismatchError,
    UnknownAgentError,
)
from fallow_coordinator.scheduler import (
    TailUnit,
    capacity_snapshot,
    choose_backup_unit,
    select_for_poll,
    select_model_for_agent,
)
from fallow_coordinator.site_relay import (
    MAX_RESPONSE_CHUNK_BYTES,
    RelayClaim,
    RelayStateError,
)
from fallow_protocol.capabilities import DeviceCaps
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
    WorkUnitLease,
)
from fallow_protocol.models import ReplicaState

logger = logging.getLogger(__name__)

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
            response = await state.registry.register_agent(req, host=host)
        except ProtocolMismatchError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except EnrollmentTokenError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        if state.config.auto_assign_on_enroll:
            # Placement runs after the token is spent and the agent row is
            # committed. A failure here must never fail the enroll, or a single-use
            # token would burn with no device_token ever returned; log and return
            # the response — the agent stays idle until a later assignment.
            try:
                await _auto_assign_on_enroll(state, response.agent_id, req.caps)
            except Exception:
                logger.exception(
                    "auto-assign on enroll failed for agent %s; enrolled without a model",
                    response.agent_id,
                )
        return response

    @router.post("/v1/agents/{agent_id}/heartbeat")
    async def heartbeat(agent_id: str, hb: Heartbeat, request: Request) -> HeartbeatResponse:
        await _authorize_self(state, agent_id, request)
        try:
            async with state.agent_liveness_lock:
                await state.registry.record_heartbeat(agent_id, hb)
        except UnknownAgentError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        # A heartbeat can flip a site agent to active/serving_paused via its
        # presence sequence; drop any relay work it still holds at once rather
        # than waiting on the claim deadline.
        await _fence_site_heartbeat(state, agent_id)
        desired = await state.registry.desired_models(agent_id)
        return HeartbeatResponse(desired_models=desired, revoked_lease_ids=(), config=None)

    @router.post("/v1/agents/{agent_id}/events", status_code=202)
    async def events(agent_id: str, event: AgentEvent, request: Request) -> Response:
        await _authorize_self(state, agent_id, request)
        await state.events.write(event)
        state.overrides.apply(event)
        # Push routing-visible state into the registry immediately so the
        # gateway also reacts now — never waits for the next heartbeat. A site
        # relay agent fences on its presence sequence and its in-flight relay work
        # is invalidated at once; direct agents keep the plain state update.
        presence = event.kind in (EventKind.USER_RETURNED, EventKind.USER_IDLE)
        if presence and not await _fence_site_presence(state, agent_id, event):
            target = AgentState.ACTIVE if event.kind is EventKind.USER_RETURNED else AgentState.IDLE
            async with state.agent_liveness_lock:
                await state.registry.set_agent_state(agent_id, target)
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

    if state.relay is not None and state.site_route is not None:
        _mount_relay_routes(router, state)
    return router


async def _auto_assign_on_enroll(state: CoordinatorState, agent_id: str, caps: DeviceCaps) -> None:
    """Assign the largest fitting model to a freshly enrolled agent (ADR 048).

    Only runs when the agent has no assignment yet, so an operator's ``flw
    assign`` is never overridden. If nothing in the registry fits the machine,
    the enroll still succeeds; the reason is logged, not raised.
    """
    if await state.registry.desired_models(agent_id):
        return  # respect operator intent: never auto-reassign
    models = await state.registry.list_models()
    chosen = select_model_for_agent(capacity_snapshot(agent_id, caps), models)
    if chosen is None:
        logger.info(
            "auto-assign on enroll: no registered model fits agent %s (%d RAM MB, %d GPUs)",
            agent_id,
            caps.ram_mb,
            len(caps.gpus),
        )
        return
    await state.registry.set_assignments(agent_id, [chosen.model_id])
    logger.info("auto-assign on enroll: assigned model %s to agent %s", chosen.model_id, agent_id)


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
    """One lease attempt: build the snapshot, gate it, and try the queue.

    With no pending work, fall through to a bounded speculative backup of an
    at-risk tail unit (ADR 056) — off unless enabled, so the poll is otherwise
    unchanged.
    """
    snapshot = await _agent_snapshot(state, agent_id)
    if snapshot is None:
        return None
    model_ids = tuple(r.model_id for r in snapshot.replicas if r.state == ReplicaState.READY)
    leasable = select_for_poll(snapshot, model_ids, state.policy)
    if not leasable:
        return None
    lease = await state.queue.lease_next(agent_id, leasable)
    if lease is None:
        lease = await _try_backup_lease(state, agent_id, leasable)
    if lease is None:
        return None
    return Response(content=lease.model_dump_json(), media_type=_JSON, status_code=200)


async def _try_backup_lease(
    state: CoordinatorState, agent_id: str, leasable: Sequence[str]
) -> WorkUnitLease | None:
    """Offer this idle agent a backup copy of an at-risk tail unit (ADR 056).

    Returns ``None`` — leaving the poll unchanged — unless the feature is enabled
    and a tail unit's holder is likely to churn before finishing. The queue
    surfaces the tail candidates and grants the crash-safe second lease; the
    survival decision (which unit, if any) is the scheduler's.
    """
    if not state.config.speculative_backup_enabled or state.churn is None:
        return None
    candidates = await state.queue.backup_candidates(
        agent_id, leasable, state.config.speculative_tail_max_units
    )
    if not candidates:
        return None
    holders = {snap.agent_id: snap for snap in await state.registry.snapshots(state.now())}
    unit_id = choose_backup_unit(
        [TailUnit(c.work_unit_id, c.holder_agent_id, c.est_duration_s) for c in candidates],
        holders,
        state.churn,
        hour=state.now().hour,
        survival_threshold=state.config.speculative_survival_threshold,
        est_unit_duration_s=state.config.churn_est_unit_duration_s,
    )
    if unit_id is None:
        return None
    return await state.queue.lease_backup(agent_id, unit_id)


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


_MAX_CLAIM_WAIT_S = 25.0  # relay-v1 bounded claim wait
_DEADLINE_MS_MAX = 300_000


class RelayFailureBody(BaseModel):
    """The agent's pre-first-byte failure report (relay-v1)."""

    model_config = ConfigDict(extra="forbid")

    presence_generation: int = Field(ge=0)
    code: str
    retryable: bool = False


async def _fence_site_presence(state: CoordinatorState, agent_id: str, event: AgentEvent) -> bool:
    """Advance the site presence fence and drop relay work; True if site-handled.

    Returns ``False`` for a direct agent or a site event without a sequence, so
    the caller falls back to the plain routing-state update. A newer user-return,
    user-idle or reclaim sequence bumps ``presence_generation`` and invalidates the
    agent's queued or claimed relay work at once.
    """
    if state.relay is None or state.site_route is None:
        return False
    route = await state.site_route(agent_id)
    if route is None:
        return False
    raw_seq = event.detail.get("sequence")
    if raw_seq is None:
        return False
    try:
        sequence = int(raw_seq)
    except ValueError:
        return False
    kind = "user_returned" if event.kind is EventKind.USER_RETURNED else "user_idle"
    async with state.agent_liveness_lock:
        generation = await state.registry.apply_presence_event(agent_id, kind, sequence)
    await state.relay.invalidate_agent(agent_id, generation, kind)
    return True


async def _fence_site_heartbeat(state: CoordinatorState, agent_id: str) -> None:
    """Invalidate a site agent's relay work when a heartbeat leaves it ineligible.

    A newer heartbeat sequence can flip the agent to active or ``serving_paused``
    without an explicit presence event. When that happens, drop its in-flight and
    queued relay work with a generation past the current fence so a claim minted
    at the old generation cannot keep serving; a later user-idle presence event
    re-enables claiming.
    """
    if state.relay is None or state.site_route is None:
        return
    route = await state.site_route(agent_id)
    if route is None:
        return
    snapshot = await _agent_snapshot(state, agent_id)
    if snapshot is None or (snapshot.state == AgentState.IDLE and not snapshot.serving_paused):
        return
    await state.relay.invalidate_agent(agent_id, route.presence_generation + 1, "heartbeat_paused")


def _mount_relay_routes(router: APIRouter, state: CoordinatorState) -> None:
    """Mount the authenticated Site Mode relay routes from relay-v1.

    Only mounted under Site Mode. Path identity is checked with the existing
    device-token auth, so a claim, response upload or failure belongs to the
    authenticated path agent.
    """
    assert state.relay is not None and state.site_route is not None
    broker = state.relay
    resolve = state.site_route

    @router.get("/v1/agents/{agent_id}/inference/claims")
    async def inference_claim(agent_id: str, request: Request, timeout_s: float = 25.0) -> Response:
        await _authorize_self(state, agent_id, request)
        route = await resolve(agent_id)
        if route is None:
            raise HTTPException(status_code=404, detail="agent is not a site relay agent")
        timeout = min(max(timeout_s, 0.0), _MAX_CLAIM_WAIT_S)
        try:
            claim = await broker.claim(agent_id, route.presence_generation, timeout)
        except RelayStateError:
            return Response(status_code=204)  # a newer presence generation fenced this waiter
        if claim is None:
            return Response(status_code=204)
        return JSONResponse(_claim_payload(state, claim))

    @router.post("/v1/agents/{agent_id}/inference/claims/{claim_id}/response")
    async def inference_response(
        agent_id: str,
        claim_id: str,
        request: Request,
        x_fallow_presence_generation: int = Header(alias="X-Fallow-Presence-Generation", ge=0),
        x_fallow_upstream_status: int = Header(
            alias="X-Fallow-Upstream-Status", default=200, ge=100, le=599
        ),
    ) -> Response:
        await _authorize_self(state, agent_id, request)
        generation = x_fallow_presence_generation
        content_type = request.headers.get("content-type", _JSON)
        started = False
        try:
            await broker.start_response(
                agent_id, claim_id, generation, x_fallow_upstream_status, content_type
            )
            started = True
            async for chunk in request.stream():
                for piece in _split_response(chunk):
                    await broker.write(agent_id, claim_id, generation, piece)
            await broker.finish(agent_id, claim_id, generation)
        except RelayStateError as exc:
            return Response(status_code=_relay_error_status(exc))
        except BaseException:
            # Upload aborted after the response opened (client disconnect,
            # cancellation): terminate the claim so it does not dangle in the
            # responding state holding relay capacity until the deadline.
            if started:
                with contextlib.suppress(RelayStateError):
                    await broker.fail(agent_id, claim_id, generation, "cancelled")
            raise
        return Response(status_code=202)

    @router.post("/v1/agents/{agent_id}/inference/claims/{claim_id}/failure")
    async def inference_failure(
        agent_id: str, claim_id: str, body: RelayFailureBody, request: Request
    ) -> Response:
        await _authorize_self(state, agent_id, request)
        # A non-retryable failure must not be replayed on another agent: record it
        # for the gateway to consume before the broker terminal wakes that request.
        if not body.retryable and state.relay_flags is not None:
            state.relay_flags.mark(claim_id)
        try:
            await broker.fail(agent_id, claim_id, body.presence_generation, body.code)
        except RelayStateError as exc:
            return Response(status_code=_relay_error_status(exc))
        return Response(status_code=202)


def _claim_payload(state: CoordinatorState, claim: RelayClaim) -> dict[str, object]:
    """Serialise a granted claim as the strict inference-claim-v1 JSON."""
    remaining_ms = round((claim.deadline - state.monotonic()) * 1000)
    deadline_ms = max(1, min(_DEADLINE_MS_MAX, remaining_ms))
    return {
        "version": 1,
        "claim_id": claim.claim_id,
        "presence_generation": claim.presence_generation,
        "replica_port": claim.replica_port,
        "method": claim.request.method,
        "path": claim.request.path,
        "content_type": claim.request.content_type,
        "body_b64": base64.b64encode(claim.request.body).decode("ascii"),
        "deadline_ms": deadline_ms,
    }


def _split_response(chunk: bytes) -> list[bytes]:
    """Split an upload chunk into relay-sized pieces (32 KiB each)."""
    if len(chunk) <= MAX_RESPONSE_CHUNK_BYTES:
        return [chunk] if chunk else []
    return [
        chunk[i : i + MAX_RESPONSE_CHUNK_BYTES]
        for i in range(0, len(chunk), MAX_RESPONSE_CHUNK_BYTES)
    ]


def _relay_error_status(exc: RelayStateError) -> int:
    """Map a relay state error to its relay-v1 HTTP status."""
    code = getattr(exc, "code", None)
    if code == "gone":
        return 410  # client left, deadline passed or a newer generation invalidated it
    if code == "unknown":
        return 404  # unknown claim
    return 409  # wrong owner, duplicate completion or invalid state
