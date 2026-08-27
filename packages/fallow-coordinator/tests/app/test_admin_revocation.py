"""The revocation routes and what a revoked identity may still do (ADR 104)."""

from __future__ import annotations

from app_helpers import (
    MODEL_ID,
    Harness,
    admin_headers,
    bearer,
    enrolled_idle_agent,
    make_manifest,
    make_register_request,
    make_replica,
    mint_enrollment_token,
    register_agent,
    send_heartbeat,
)

from fallow_coordinator.httpauth import REVOKED_DEVICE_TOKEN_DETAIL


async def _tokens(harness: Harness) -> list[dict]:
    resp = await harness.client.get("/v1/admin/enrollment_tokens", headers=admin_headers())
    assert resp.status_code == 200, resp.text
    return list(resp.json())


async def test_enrollment_tokens_listing_names_tokens_without_leaking_them(
    harness: Harness,
) -> None:
    token = await mint_enrollment_token(harness.client)

    (row,) = await _tokens(harness)
    assert row["state"] == "outstanding"
    assert row["mode"] == "legacy"
    assert len(row["token_id"]) == 12
    assert token not in str(row)


async def test_revoked_enrollment_token_cannot_enrol(harness: Harness) -> None:
    token = await mint_enrollment_token(harness.client)
    (row,) = await _tokens(harness)

    resp = await harness.client.delete(
        f"/v1/admin/enrollment_tokens/{row['token_id']}", headers=admin_headers()
    )
    assert resp.status_code == 204

    body = make_register_request(token).model_dump(mode="json")
    failed = await harness.client.post("/v1/agents/register", json=body)
    # Exactly what a token an agent already spent gets.
    assert failed.status_code == 401
    assert (await _tokens(harness))[0]["state"] == "revoked"


async def test_revoking_an_unknown_token_is_404(harness: Harness) -> None:
    resp = await harness.client.delete(
        "/v1/admin/enrollment_tokens/000000000000", headers=admin_headers()
    )
    assert resp.status_code == 404


async def test_revoked_device_token_is_rejected_everywhere(harness: Harness) -> None:
    agent_id, device_token = await enrolled_idle_agent(harness.client)
    await harness.state.registry.put_model(make_manifest(MODEL_ID), "weights")

    resp = await harness.client.post(f"/v1/admin/agents/{agent_id}/revoke", headers=admin_headers())
    assert resp.status_code == 204

    beat = await send_heartbeat(harness.client, agent_id, device_token)
    assert beat.status_code == 401
    work = await harness.client.get(f"/v1/agents/{agent_id}/work", headers=bearer(device_token))
    assert work.status_code == 401
    blob = await harness.client.get(f"/v1/models/{MODEL_ID}/manifest", headers=bearer(device_token))
    assert blob.status_code == 401


async def test_a_revoked_401_is_distinguishable_from_an_unknown_one(harness: Harness) -> None:
    """The detail is what the Go agent reads before it writes a permanent marker.

    A coordinator that lost its database refuses every desk with "invalid device
    token". If that were indistinguishable from a revocation, one restore would
    brick the fleet until somebody visited each machine (ADR 104).
    """
    agent_id, device_token = await enrolled_idle_agent(harness.client)

    unknown = await send_heartbeat(harness.client, agent_id, "not-a-token-anyone-issued")
    assert unknown.status_code == 401
    assert unknown.json()["detail"] == "invalid device token"

    resp = await harness.client.post(f"/v1/admin/agents/{agent_id}/revoke", headers=admin_headers())
    assert resp.status_code == 204

    revoked = await send_heartbeat(harness.client, agent_id, device_token)
    assert revoked.status_code == 401
    assert revoked.json()["detail"] == REVOKED_DEVICE_TOKEN_DETAIL


async def test_revoke_drops_replicas_from_routing_and_assignments(harness: Harness) -> None:
    agent_id, _device_token = await enrolled_idle_agent(
        harness.client, replicas=(make_replica(MODEL_ID),)
    )
    await harness.state.registry.put_model(make_manifest(MODEL_ID), "weights")
    await harness.state.registry.set_assignments(agent_id, [MODEL_ID])
    assert await harness.state.registry.replica_endpoints(MODEL_ID, harness.clock())

    resp = await harness.client.post(f"/v1/admin/agents/{agent_id}/revoke", headers=admin_headers())
    assert resp.status_code == 204

    assert await harness.state.registry.replica_endpoints(MODEL_ID, harness.clock()) == ()
    assert await harness.state.registry.desired_models(agent_id) == ()
    listed = await harness.client.get("/v1/admin/agents", headers=admin_headers())
    assert listed.json() == []


async def test_revoked_agents_have_a_listing_of_their_own(harness: Harness) -> None:
    """`GET /agents` is the routing view; this is the operator's.

    Outside Site Mode nothing else would show that a machine was revoked rather
    than never enrolled, which is the whole point of asking.
    """
    agent_id, _ = await enrolled_idle_agent(harness.client)
    empty = await harness.client.get("/v1/admin/agents/revoked", headers=admin_headers())
    assert empty.status_code == 200 and empty.json() == []

    resp = await harness.client.post(f"/v1/admin/agents/{agent_id}/revoke", headers=admin_headers())
    assert resp.status_code == 204

    assert (await harness.client.get("/v1/admin/agents", headers=admin_headers())).json() == []
    listed = await harness.client.get("/v1/admin/agents/revoked", headers=admin_headers())
    assert listed.status_code == 200, listed.text
    (row,) = listed.json()
    assert row["agent_id"] == agent_id
    assert row["hostname"] and row["revoked_at"]


async def test_revoke_is_idempotent_and_unknown_agents_are_404(harness: Harness) -> None:
    token = await mint_enrollment_token(harness.client)
    agent_id, _ = await register_agent(harness.client, token)

    for _ in range(2):
        resp = await harness.client.post(
            f"/v1/admin/agents/{agent_id}/revoke", headers=admin_headers()
        )
        assert resp.status_code == 204

    missing = await harness.client.post(
        "/v1/admin/agents/no-such-agent/revoke", headers=admin_headers()
    )
    assert missing.status_code == 404


async def test_a_malformed_token_id_is_not_reported_as_already_spent(harness: Harness) -> None:
    resp = await harness.client.delete(
        "/v1/admin/enrollment_tokens/not-a-token-id", headers=admin_headers()
    )
    assert resp.status_code == 404
    assert "is not a token id" in resp.json()["detail"]


async def test_revocation_routes_require_admin(harness: Harness) -> None:
    agent_id, _ = await enrolled_idle_agent(harness.client)
    assert (await harness.client.get("/v1/admin/enrollment_tokens")).status_code == 401
    assert (
        await harness.client.delete("/v1/admin/enrollment_tokens/000000000000")
    ).status_code == 401
    assert (
        await harness.client.post(
            f"/v1/admin/agents/{agent_id}/revoke", headers=bearer("not-admin")
        )
    ).status_code == 401
    assert (await harness.client.get("/v1/admin/agents/revoked")).status_code == 401
