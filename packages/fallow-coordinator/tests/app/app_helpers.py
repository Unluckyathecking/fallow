"""Deterministic builders and a frozen clock for coordinator app tests.

No real network beyond the in-process ASGI transport; no llama-server; no GPU.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from fallow_coordinator.app import CoordinatorConfig
from fallow_coordinator.app.state import CoordinatorState
from fallow_protocol.capabilities import DeviceCaps, OsFamily, WorkerKind
from fallow_protocol.messages import (
    AgentEvent,
    AgentState,
    EventKind,
    Heartbeat,
    RegisterRequest,
    WorkMetrics,
    WorkResult,
    WorkResultStatus,
)
from fallow_protocol.models import ModelManifest, ReplicaState, ReplicaStatus
from fallow_protocol.version import PROTOCOL_VERSION

START = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
SHA_ZERO = "0" * 64
ADMIN_KEY = "admin-secret-key"
MODEL_ID = "qwen2.5-7b"


class FakeClock:
    """A frozen (advanceable) aware-UTC clock injected as ``now``."""

    def __init__(self, start: datetime = START) -> None:
        self._t = start

    def __call__(self) -> datetime:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t = self._t + timedelta(seconds=seconds)


@dataclass
class Harness:
    """One coordinator app plus its ASGI client, injected clock, and config."""

    client: httpx.AsyncClient
    clock: FakeClock
    config: CoordinatorConfig
    state: CoordinatorState


def admin_headers(key: str = ADMIN_KEY) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def make_caps(hostname: str = "pc1") -> DeviceCaps:
    return DeviceCaps(
        hostname=hostname,
        os=OsFamily.LINUX,
        os_version="6.1",
        cpu_model="test-cpu",
        cpu_cores=8,
        ram_mb=16384,
        disk_free_mb=100000,
        agent_version="0.1.0",
    )


def make_register_request(
    token: str, hostname: str = "pc1", protocol_version: int = PROTOCOL_VERSION
) -> RegisterRequest:
    return RegisterRequest(
        enrollment_token=token, protocol_version=protocol_version, caps=make_caps(hostname)
    )


def make_replica(
    model_id: str = MODEL_ID, state: ReplicaState = ReplicaState.READY
) -> ReplicaStatus:
    return ReplicaStatus(model_id=model_id, port=8080, state=state, inflight=0)


def make_heartbeat(
    agent_id: str,
    state: AgentState = AgentState.IDLE,
    replicas: tuple[ReplicaStatus, ...] = (),
) -> Heartbeat:
    return Heartbeat(
        agent_id=agent_id,
        seq=1,
        sent_at=START,
        protocol_version=PROTOCOL_VERSION,
        state=state,
        user_idle_s=300.0,
        cpu_percent=5.0,
        mem_available_mb=8192,
        replicas=replicas,
    )


def make_manifest(model_id: str = MODEL_ID, kind: WorkerKind = WorkerKind.EMBED) -> ModelManifest:
    return ModelManifest(
        model_id=model_id,
        family=model_id.split("-")[0],
        quant="Q4_K_M",
        worker_kind=kind,
        file_name=f"{model_id}.gguf",
        sha256=SHA_ZERO,
        size_bytes=1024,
    )


def make_user_returned(agent_id: str) -> AgentEvent:
    return AgentEvent(agent_id=agent_id, kind=EventKind.USER_RETURNED, at=START)


def make_success_result(work_unit_id: str) -> WorkResult:
    return WorkResult(
        work_unit_id=work_unit_id,
        status=WorkResultStatus.SUCCEEDED,
        result_ref=f"result://{work_unit_id}",
        metrics=WorkMetrics(duration_s=1.0, items=1),
    )


# ── async flow helpers (drive the app over the ASGI client) ──────────────────


async def mint_enrollment_token(client: httpx.AsyncClient) -> str:
    resp = await client.post("/v1/admin/enrollment_tokens", headers=admin_headers())
    assert resp.status_code == 201, resp.text
    return str(resp.json()["token"])


async def register_agent(
    client: httpx.AsyncClient, token: str, hostname: str = "pc1"
) -> tuple[str, str]:
    """Register an agent; returns ``(agent_id, device_token)``."""
    body = make_register_request(token, hostname).model_dump(mode="json")
    resp = await client.post("/v1/agents/register", json=body)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    return str(data["agent_id"]), str(data["device_token"])


async def send_heartbeat(
    client: httpx.AsyncClient,
    agent_id: str,
    device_token: str,
    *,
    state: AgentState = AgentState.IDLE,
    replicas: tuple[ReplicaStatus, ...] = (),
) -> httpx.Response:
    body = make_heartbeat(agent_id, state, replicas).model_dump(mode="json")
    return await client.post(
        f"/v1/agents/{agent_id}/heartbeat", json=body, headers=bearer(device_token)
    )


async def enrolled_idle_agent(
    client: httpx.AsyncClient, replicas: tuple[ReplicaStatus, ...] = ()
) -> tuple[str, str]:
    """Full path: mint token → register → heartbeat IDLE. Returns ids."""
    token = await mint_enrollment_token(client)
    agent_id, device_token = await register_agent(client, token)
    await send_heartbeat(client, agent_id, device_token, replicas=replicas)
    return agent_id, device_token
