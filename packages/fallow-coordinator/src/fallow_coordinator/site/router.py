from __future__ import annotations

import base64
import hashlib
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import APIRouter, Request

from fallow_coordinator.app.config import CoordinatorConfig
from fallow_coordinator.app.deps import authenticate_admin
from fallow_coordinator.app.state import CoordinatorState
from fallow_coordinator.site.models import JoinBundlesRequest, JoinBundleV1
from fallow_protocol.base import FallowModel
from fallow_protocol.messages import AgentSnapshot, AgentState
from fallow_protocol.models import ReplicaState

TokenFactory = Callable[[], Awaitable[str]]

# Presence as the fleet view reports it: the registry's routing-visible agent
# state, widened with the two conditions that are presence facts rather than
# states — a machine whose user reclaimed it, and one that stopped heartbeating.
_OFFLINE = "offline"
_RECLAIMED = "reclaimed"
_NEVER_CLAIMED = "none"


class SiteAgentStatusV1(FallowModel):
    """One Site Mode agent's live fleet status.

    Carries no token, pin or join material: every field is either an identifier
    the operator already has or a derived liveness fact.
    """

    agent_id: str
    enrollment_mode: str
    transport: str
    heartbeat_age_s: float
    presence_state: str
    presence_generation: int
    available: bool
    ready_replicas: int
    last_claim: str
    last_claim_code: str | None


def _presence_state(snapshot: AgentSnapshot | None) -> str:
    if snapshot is None:
        return _OFFLINE
    if snapshot.serving_paused:
        return _RECLAIMED
    return snapshot.state.value


def _available(snapshot: AgentSnapshot | None) -> bool:
    """Whether routing would consider this agent right now.

    The model-independent half of ``registry.site_eligible``: fresh, idle and
    unpaused. Whether a *particular* model can be served is the replica count's
    job, not this flag's.
    """
    if snapshot is None:
        return False
    return (
        snapshot.state == AgentState.IDLE and not snapshot.suspect and not snapshot.serving_paused
    )


def _ready_replicas(snapshot: AgentSnapshot | None) -> int:
    if snapshot is None:
        return 0
    return sum(1 for replica in snapshot.replicas if replica.state == ReplicaState.READY)


def _spki_pin(certfile: Path) -> str:
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    cert = x509.load_pem_x509_certificate(certfile.read_bytes())
    der = cert.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    return "sha256/" + base64.b64encode(hashlib.sha256(der).digest()).decode("ascii")


def build_site_admin_router(
    settings: CoordinatorConfig, create_site_token: TokenFactory
) -> APIRouter:
    site = settings.site
    assert site.tls_certfile is not None and site.site_id is not None
    site_id = site.site_id
    # Pin the certificate the listener loads at startup, once. Reading it per
    # request would emit a pin for a cert rotated on disk while uvicorn keeps
    # serving the old one, breaking the agent's mandatory pin check on every
    # freshly minted bundle until the server restarts.
    pin = _spki_pin(site.tls_certfile)
    router = APIRouter(prefix="/v1/admin/site")

    @router.post("/join-bundles", status_code=201)
    async def join_bundles(
        body: JoinBundlesRequest, request: Request
    ) -> dict[str, list[JoinBundleV1]]:
        await authenticate_admin(
            request.app.state.coordinator, request.headers.get("authorization")
        )
        return {
            "bundles": [
                JoinBundleV1(
                    site_id=site_id,
                    coordinator_urls=site.public_urls,
                    coordinator_spki_sha256=(pin,),
                    enrollment_token=await create_site_token(),
                    mdns_service=site.mdns_service,
                )
                for _ in range(body.count)
            ]
        }

    @router.get("/status")
    async def fleet_status(request: Request) -> dict[str, list[SiteAgentStatusV1]]:
        await authenticate_admin(
            request.app.state.coordinator, request.headers.get("authorization")
        )
        state: CoordinatorState = request.app.state.coordinator
        now = state.now()
        live = await state.registry.snapshots(now)
        snapshots = {snapshot.agent_id: snapshot for snapshot in live}
        rows = []
        for entry in await state.registry.site_fleet(now):
            snapshot = snapshots.get(entry.agent_id)
            claim = (
                await state.relay.last_claim(entry.agent_id) if state.relay is not None else None
            )
            rows.append(
                SiteAgentStatusV1(
                    agent_id=entry.agent_id,
                    enrollment_mode=entry.enrollment_mode.value,
                    transport=entry.transport.value,
                    heartbeat_age_s=round(entry.heartbeat_age_s, 1),
                    presence_state=_presence_state(snapshot),
                    presence_generation=entry.presence_generation,
                    available=_available(snapshot),
                    ready_replicas=_ready_replicas(snapshot),
                    last_claim=claim.outcome if claim is not None else _NEVER_CLAIMED,
                    last_claim_code=claim.code if claim is not None else None,
                )
            )
        return {"agents": rows}

    return router
