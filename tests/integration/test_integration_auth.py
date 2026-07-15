"""Scenario 6 — auth boundaries across all three surfaces: a bad device token is
rejected on agent routes, a bad api key on the gateway, and a bad admin key on
the admin API.
"""

from __future__ import annotations

import pytest
from conftest import Harness, HarnessFactory
from integration_helpers import (
    CHAT_MODEL,
    bearer,
    credentialed_client,
    enroll_agent,
    make_heartbeat,
    mint_enrollment_token,
)

from fallow_agent.heartbeat import CoordinatorAuthError


async def test_agent_route_rejects_bad_device_token(make_harness: HarnessFactory) -> None:
    harness: Harness = await make_harness()
    raw = harness.client
    agent = await enroll_agent(raw, await mint_enrollment_token(raw))
    assert agent.agent_id is not None

    body = make_heartbeat(agent.agent_id).model_dump(mode="json")
    resp = await raw.post(
        f"/v1/agents/{agent.agent_id}/heartbeat", json=body, headers=bearer("not-a-real-token")
    )
    assert resp.status_code == 401

    # The real A5 client maps that 401 onto a typed auth error.
    impostor = credentialed_client(raw, agent.agent_id, "not-a-real-token")
    with pytest.raises(CoordinatorAuthError):
        await impostor.heartbeat(make_heartbeat(agent.agent_id))


async def test_gateway_rejects_bad_api_key(make_harness: HarnessFactory) -> None:
    harness: Harness = await make_harness()
    resp = await harness.client.post(
        "/v1/chat/completions", json={"model": CHAT_MODEL}, headers=bearer("nope")
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["type"] == "invalid_request_error"


async def test_admin_rejects_bad_admin_key(make_harness: HarnessFactory) -> None:
    harness: Harness = await make_harness()
    bad = await harness.client.get("/v1/admin/agents", headers=bearer("wrong-admin-key"))
    assert bad.status_code == 401
    missing = await harness.client.get("/v1/admin/agents")
    assert missing.status_code == 401
