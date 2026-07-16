from __future__ import annotations

import json

from app_helpers import admin_headers, enrolled_idle_agent

from fallow_coordinator.app.metrics import GatewayCounters, format_metrics
from fallow_protocol.capabilities import DeviceCaps, OsFamily
from fallow_protocol.messages import AgentSnapshot, AgentState
from fallow_protocol.models import ReplicaState, ReplicaStatus


def _snapshot(
    *,
    agent_id: str,
    state: AgentState,
    suspect: bool,
    replicas: tuple[ReplicaStatus, ...],
) -> AgentSnapshot:
    return AgentSnapshot(
        agent_id=agent_id,
        host="127.0.0.1",
        state=state,
        suspect=suspect,
        caps=DeviceCaps(
            hostname=agent_id,
            os=OsFamily.LINUX,
            os_version="6.1",
            cpu_model="test",
            cpu_cores=4,
            ram_mb=8192,
            disk_free_mb=1000,
            agent_version="0.1.0",
        ),
        mem_available_mb=4096,
        replicas=replicas,
    )


def test_formatter_exposes_fleet_gateway_and_inflight_metrics() -> None:
    ready = ReplicaStatus(model_id='chat"model', port=8000, state=ReplicaState.READY, inflight=2)
    stopped = ReplicaStatus(
        model_id='chat"model', port=8001, state=ReplicaState.STOPPED, inflight=0
    )
    snapshots = (
        _snapshot(agent_id="a1", state=AgentState.IDLE, suspect=False, replicas=(ready, stopped)),
        _snapshot(agent_id="a2", state=AgentState.ACTIVE, suspect=True, replicas=()),
    )

    body = format_metrics(snapshots, GatewayCounters(served=7, shed=2, error=1, retried=3))

    assert "# TYPE fallow_agents_total gauge" in body
    assert "fallow_agents_total 2" in body
    assert 'fallow_agents_total{state="idle"} 1' in body
    assert 'fallow_agents_total{state="active"} 1' in body
    assert 'fallow_agents_total{state="suspect"} 1' in body
    assert 'fallow_replicas_total{model_id="chat\\"model",state="ready"} 1' in body
    assert 'fallow_replicas_total{model_id="chat\\"model",state="stopped"} 1' in body
    assert 'fallow_gateway_requests_total{status="served"} 7' in body
    assert 'fallow_gateway_requests_total{status="shed"} 2' in body
    assert 'fallow_gateway_requests_total{status="error"} 1' in body
    assert "fallow_gateway_retried_total 3" in body
    assert "fallow_inflight_total 2" in body
    assert body.endswith("\n")


async def test_metrics_route_is_admin_protected_and_reads_gateway_log(harness) -> None:
    await enrolled_idle_agent(harness.client)
    entries = (
        {"status": "served", "retried": True},
        {"status": "shed", "retried": False},
        {"status": "error", "retried": True},
        {"status": "cancelled", "retried": False},
    )
    harness.config.gateway_log_path.write_text(
        "\n".join(json.dumps(entry) for entry in entries) + "\nnot-json\n",
        encoding="utf-8",
    )

    missing = await harness.client.get("/metrics")
    wrong = await harness.client.get("/metrics", headers=admin_headers("wrong"))
    response = await harness.client.get("/metrics", headers=admin_headers())

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"
    assert 'fallow_agents_total{state="idle"} 1' in response.text
    assert 'fallow_gateway_requests_total{status="served"} 1' in response.text
    assert 'fallow_gateway_requests_total{status="shed"} 1' in response.text
    assert 'fallow_gateway_requests_total{status="error"} 1' in response.text
    assert "fallow_gateway_retried_total 2" in response.text
