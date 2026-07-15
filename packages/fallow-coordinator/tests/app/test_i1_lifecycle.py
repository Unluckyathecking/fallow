"""Agent lifecycle: register → heartbeat → visible in admin, plus auth rejection."""

from __future__ import annotations

from app_helpers import (
    admin_headers,
    bearer,
    make_heartbeat,
    make_register_request,
    mint_enrollment_token,
    register_agent,
    send_heartbeat,
)
from conftest import Harness

from fallow_protocol.messages import AgentSnapshot, AgentState


async def test_register_heartbeat_visible_in_admin(harness: Harness) -> None:
    token = await mint_enrollment_token(harness.client)
    agent_id, device_token = await register_agent(harness.client, token, hostname="desk-7")

    hb = await send_heartbeat(harness.client, agent_id, device_token, state=AgentState.IDLE)
    assert hb.status_code == 200
    body = hb.json()
    assert body["desired_models"] == []
    assert body["revoked_lease_ids"] == []
    assert body["config"] is None

    resp = await harness.client.get("/v1/admin/agents", headers=admin_headers())
    assert resp.status_code == 200
    snapshots = [AgentSnapshot.model_validate(item) for item in resp.json()]
    assert [s.agent_id for s in snapshots] == [agent_id]
    assert snapshots[0].caps.hostname == "desk-7"
    assert snapshots[0].state == AgentState.IDLE


async def test_used_enrollment_token_is_single_use(harness: Harness) -> None:
    token = await mint_enrollment_token(harness.client)
    await register_agent(harness.client, token)
    body = make_register_request(token, hostname="pc2").model_dump(mode="json")
    resp = await harness.client.post("/v1/agents/register", json=body)
    assert resp.status_code == 401


async def test_register_rejects_protocol_mismatch(harness: Harness) -> None:
    token = await mint_enrollment_token(harness.client)
    body = make_register_request(token, protocol_version=999).model_dump(mode="json")
    resp = await harness.client.post("/v1/agents/register", json=body)
    assert resp.status_code == 409


async def test_heartbeat_rejected_with_wrong_token(harness: Harness) -> None:
    token = await mint_enrollment_token(harness.client)
    agent_id, _device_token = await register_agent(harness.client, token)
    body = make_heartbeat(agent_id).model_dump(mode="json")
    resp = await harness.client.post(
        f"/v1/agents/{agent_id}/heartbeat", json=body, headers=bearer("not-a-real-token")
    )
    assert resp.status_code == 401


async def test_heartbeat_missing_bearer_is_401(harness: Harness) -> None:
    token = await mint_enrollment_token(harness.client)
    agent_id, _device_token = await register_agent(harness.client, token)
    body = make_heartbeat(agent_id).model_dump(mode="json")
    resp = await harness.client.post(f"/v1/agents/{agent_id}/heartbeat", json=body)
    assert resp.status_code == 401


async def test_user_returned_event_sets_registry_state_immediately(harness: Harness) -> None:
    """Gateway routing must react to user events without waiting for a heartbeat."""
    token = await mint_enrollment_token(harness.client)
    agent_id, device_token = await register_agent(harness.client, token)
    await send_heartbeat(harness.client, agent_id, device_token, state=AgentState.IDLE)

    resp = await harness.client.post(
        f"/v1/agents/{agent_id}/events",
        headers=bearer(device_token),
        json={
            "agent_id": agent_id,
            "kind": "user_returned",
            "at": "2026-07-15T12:00:00Z",
            "detail": {},
        },
    )
    assert resp.status_code == 202

    admin = await harness.client.get("/v1/admin/agents", headers=admin_headers())
    snapshots = [AgentSnapshot.model_validate(item) for item in admin.json()]
    assert snapshots[0].state == AgentState.ACTIVE
