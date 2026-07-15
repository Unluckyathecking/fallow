"""Builders and wire-flow helpers for the I3 end-to-end integration suite.

Everything here composes ONLY public APIs: the real coordinator app (I1) over an
in-process ``ASGITransport`` and the real A5 ``CoordinatorClient`` / I2
``resolve_identity`` as the agent uplink. No sockets for the coordinator, no
llama-server, no GPU. Message builders mirror the wire types by field name.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from fallow_agent.heartbeat import CoordinatorClient
from fallow_agent.main.result_upload import ResultUploader
from fallow_coordinator.app import CoordinatorConfig
from fallow_protocol.capabilities import DeviceCaps, OsFamily, WorkerKind
from fallow_protocol.messages import (
    AgentEvent,
    AgentSnapshot,
    AgentState,
    EventKind,
    Heartbeat,
    HeartbeatResponse,
    JobStatus,
    JobSubmit,
    RegisterRequest,
    WorkMetrics,
    WorkResult,
    WorkResultStatus,
    WorkUnitLease,
)
from fallow_protocol.models import ModelManifest, ReplicaState, ReplicaStatus
from fallow_protocol.version import PROTOCOL_VERSION

START = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
SHA_ZERO = "0" * 64
ADMIN_KEY = "admin-secret-key"
COORD_BASE = "http://coord"
EMBED_MODEL = "bge-small"
CHAT_MODEL = "qwen2.5-7b"
LOOPBACK = "127.0.0.1"


class FakeClock:
    """A frozen, advanceable aware-UTC clock injected into ``create_app``."""

    def __init__(self, start: datetime = START) -> None:
        self._t = start

    def __call__(self) -> datetime:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t = self._t + timedelta(seconds=seconds)


@dataclass
class Harness:
    """One live coordinator app plus the raw ASGI client and its clock/config."""

    client: httpx.AsyncClient
    clock: FakeClock
    config: CoordinatorConfig


HarnessFactory = Callable[..., Awaitable[Harness]]


# ── header + message builders ────────────────────────────────────────────────


def admin_headers(key: str = ADMIN_KEY) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def make_caps(hostname: str = "pc1", agent_version: str = "0.1.0") -> DeviceCaps:
    return DeviceCaps(
        hostname=hostname,
        os=OsFamily.LINUX,
        os_version="6.1",
        cpu_model="test-cpu",
        cpu_cores=8,
        ram_mb=16384,
        disk_free_mb=100000,
        agent_version=agent_version,
    )


def make_register_request(token: str, hostname: str = "pc1") -> RegisterRequest:
    return RegisterRequest(
        enrollment_token=token, protocol_version=PROTOCOL_VERSION, caps=make_caps(hostname)
    )


def make_replica(
    model_id: str,
    port: int,
    state: ReplicaState = ReplicaState.READY,
    inflight: int = 0,
) -> ReplicaStatus:
    return ReplicaStatus(model_id=model_id, port=port, state=state, inflight=inflight)


def make_heartbeat(
    agent_id: str,
    *,
    state: AgentState = AgentState.IDLE,
    replicas: tuple[ReplicaStatus, ...] = (),
    seq: int = 1,
) -> Heartbeat:
    return Heartbeat(
        agent_id=agent_id,
        seq=seq,
        sent_at=START,
        protocol_version=PROTOCOL_VERSION,
        state=state,
        user_idle_s=300.0,
        cpu_percent=5.0,
        mem_available_mb=8192,
        replicas=replicas,
    )


def make_manifest(model_id: str, kind: WorkerKind = WorkerKind.EMBED) -> ModelManifest:
    return ModelManifest(
        model_id=model_id,
        family=model_id.split("-")[0],
        quant="Q4_K_M",
        worker_kind=kind,
        file_name=f"{model_id}.gguf",
        sha256=SHA_ZERO,
        size_bytes=1024,
    )


def make_event(agent_id: str, kind: EventKind) -> AgentEvent:
    return AgentEvent(agent_id=agent_id, kind=kind, at=START)


def fake_embed_result(lease: WorkUnitLease, result_ref: str | None = None) -> WorkResult:
    """The fake embed worker: turn a lease into a SUCCEEDED result (no llama)."""
    return WorkResult(
        work_unit_id=lease.work_unit_id,
        status=WorkResultStatus.SUCCEEDED,
        result_ref=result_ref or f"result://{lease.work_unit_id}",
        metrics=WorkMetrics(duration_s=1.0, items=1),
    )


# ── admin-route flows (raw ASGI client, admin bearer) ────────────────────────


async def mint_enrollment_token(raw: httpx.AsyncClient) -> str:
    resp = await raw.post("/v1/admin/enrollment_tokens", headers=admin_headers())
    assert resp.status_code == 201, resp.text
    return str(resp.json()["token"])


async def create_api_key(
    raw: httpx.AsyncClient, name: str, allowlist: list[str] | None = None
) -> str:
    body: dict[str, object] = {"name": name}
    if allowlist is not None:
        body["model_allowlist"] = allowlist
    resp = await raw.post("/v1/admin/api_keys", json=body, headers=admin_headers())
    assert resp.status_code == 201, resp.text
    return str(resp.json()["key"])


async def register_model(raw: httpx.AsyncClient, manifest: ModelManifest, blob_path: str) -> None:
    resp = await raw.post(
        "/v1/admin/models",
        json={"manifest": manifest.model_dump(mode="json"), "blob_path": blob_path},
        headers=admin_headers(),
    )
    assert resp.status_code == 201, resp.text


async def assign_model(raw: httpx.AsyncClient, model_id: str, agent_ids: list[str]) -> None:
    resp = await raw.put(
        "/v1/admin/assignments",
        json={"model_id": model_id, "agent_ids": agent_ids},
        headers=admin_headers(),
    )
    assert resp.status_code == 204, resp.text


async def submit_job(raw: httpx.AsyncClient, job: JobSubmit) -> JobStatus:
    resp = await raw.post(
        "/v1/admin/jobs", json=job.model_dump(mode="json"), headers=admin_headers()
    )
    assert resp.status_code == 201, resp.text
    return JobStatus.model_validate(resp.json())


async def job_status(raw: httpx.AsyncClient, job_id: str) -> JobStatus:
    resp = await raw.get(f"/v1/admin/jobs/{job_id}", headers=admin_headers())
    assert resp.status_code == 200, resp.text
    return JobStatus.model_validate(resp.json())


async def list_agents(raw: httpx.AsyncClient) -> list[AgentSnapshot]:
    resp = await raw.get("/v1/admin/agents", headers=admin_headers())
    assert resp.status_code == 200, resp.text
    return [AgentSnapshot.model_validate(item) for item in resp.json()]


# ── agent-route flows (real A5 CoordinatorClient) ────────────────────────────


def make_agent_client(raw: httpx.AsyncClient) -> CoordinatorClient:
    """Wrap the shared ASGI transport in the real A5 client (no creds yet)."""
    return CoordinatorClient(base_url=COORD_BASE, client=raw)


def credentialed_client(
    raw: httpx.AsyncClient, agent_id: str, device_token: str
) -> CoordinatorClient:
    """A real A5 client already holding an enrolled agent's credentials."""
    return CoordinatorClient(
        base_url=COORD_BASE, client=raw, agent_id=agent_id, device_token=device_token
    )


async def enroll_agent(
    raw: httpx.AsyncClient, token: str, hostname: str = "pc1"
) -> CoordinatorClient:
    """Register through the real A5 client; returns a credentialed client."""
    client = make_agent_client(raw)
    await client.register(make_register_request(token, hostname))
    return client


async def heartbeat(
    client: CoordinatorClient,
    *,
    state: AgentState = AgentState.IDLE,
    replicas: tuple[ReplicaStatus, ...] = (),
    seq: int = 1,
) -> HeartbeatResponse:
    assert client.agent_id is not None
    return await client.heartbeat(
        make_heartbeat(client.agent_id, state=state, replicas=replicas, seq=seq)
    )


async def push_event(client: CoordinatorClient, kind: EventKind) -> None:
    assert client.agent_id is not None
    await client.push_event(make_event(client.agent_id, kind))


async def fetch_input(raw: httpx.AsyncClient, input_url: str, device_token: str) -> bytes:
    resp = await raw.get(f"/v1/work_units/{input_url}/input", headers=bearer(device_token))
    assert resp.status_code == 200, resp.text
    return resp.content


async def upload_result(
    raw: httpx.AsyncClient,
    agent: CoordinatorClient,
    lease: WorkUnitLease,
    payload: bytes,
) -> WorkResult:
    """Upload bytes through the real typed client and build the matching result."""
    assert agent.agent_id is not None
    assert agent.device_token is not None
    uploader = ResultUploader(
        base_url=COORD_BASE,
        agent_id=agent.agent_id,
        device_token=agent.device_token,
        client=raw,
    )
    result_ref = await uploader.upload(lease, payload)
    return fake_embed_result(lease, result_ref)


async def fetch_result_payload(raw: httpx.AsyncClient, work_unit_id: str) -> bytes:
    response = await raw.get(
        f"/v1/admin/work_units/{work_unit_id}/payload", headers=admin_headers()
    )
    assert response.status_code == 200, response.text
    return response.content
