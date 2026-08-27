"""Revoking a serving desk, end to end against the real Go agent (ADR 104).

The unit tests prove the route and the marker in isolation. This proves the
thing an operator actually needs on the day a laptop goes missing: one enrolled,
serving desk is revoked mid-flight, routing stops at once, the daemon takes
itself down instead of hammering the coordinator with a rejected token, and both
the fleet view and the desk's own doctor say why.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from integration_helpers import make_register_request

from site_mode.site_harness import (
    SiteCoordinator,
    assign_model,
    chat_once,
    create_api_key,
    doctor,
    list_agents,
    llama_command,
    mint_join_bundle_and_token_id,
    mint_join_bundle_via_flw,
    register_chat_model,
    run_site_daemon,
    wait_enrolled,
    wait_process_exit,
    wait_replica_ready,
    write_agent_toml,
)

pytestmark = pytest.mark.asyncio


async def revoke(coord: SiteCoordinator, agent_id: str) -> None:
    response = await coord.client.post(
        f"/v1/admin/agents/{agent_id}/revoke", headers=coord.admin_headers()
    )
    assert response.status_code == 204, response.text


async def fleet_row(coord: SiteCoordinator, agent_id: str) -> dict:
    response = await coord.client.get("/v1/admin/site/status", headers=coord.admin_headers())
    assert response.status_code == 200, response.text
    rows = {row["agent_id"]: row for row in response.json()["agents"]}
    return rows[agent_id]


async def test_revoking_a_serving_desk_stops_it_and_parks_it(
    coordinator: SiteCoordinator, site_binary: Path, tmp_path: Path
) -> None:
    coord = coordinator
    blob = tmp_path / "model.gguf"
    blob.write_bytes(b"fake-gguf-bytes-for-the-pilot")
    await register_chat_model(coord, blob)
    key = await create_api_key(coord)

    join = await asyncio.to_thread(mint_join_bundle_via_flw, coord, tmp_path / "join")
    config = tmp_path / "agent.toml"
    state = tmp_path / "agent-state.json"
    write_agent_toml(
        config,
        join_bundle=join,
        state_path=state,
        cache_dir=tmp_path / "cache",
        llama_binary=llama_command(),
    )

    async with run_site_daemon(site_binary, config, state) as daemon:
        agent_id = await wait_enrolled(coord)
        await assign_model(coord, [agent_id])
        await wait_replica_ready(coord, agent_id)
        assert (await chat_once(coord, key)).status_code == 200, daemon.stderr

        await revoke(coord, agent_id)

        # Routing stops on the revocation itself, not on the next heartbeat.
        shed = await chat_once(coord, key)
        assert shed.status_code == 503, shed.text
        assert await list_agents(coord) == []

        # The desk parks itself: it stops rather than retrying a dead token, and
        # it exits 0 so the Scheduled Task's restart-on-failure leaves it down.
        rc = await wait_process_exit(daemon)
        assert rc == 0, daemon.stderr
        assert "revoke" in daemon.stderr or "rejected" in daemon.stderr

    # The fleet view names it, rather than showing a desk that merely went quiet.
    row = await fleet_row(coord, agent_id)
    assert row["presence_state"] == "revoked"
    assert row["available"] is False

    # And doctor, offline, tells the person at the desk what happened.
    report = await doctor(site_binary, config)
    assert report["identity"]["ok"] is False, report
    assert "device token rejected by the coordinator" in report["identity"]["detail"]


async def test_the_printed_token_id_is_the_one_that_revokes_that_join_file(
    coordinator: SiteCoordinator, tmp_path: Path
) -> None:
    """The seam between the two halves of ADR 104's token-id story.

    The CLI derives the id from the token it just wrote to a file; the
    coordinator derives it from the digest it stored. Nothing carries the id
    between them, so only a test that mints, reads the printed line, and revokes
    by exactly that string proves the two derivations agree — and that revoking
    it is what stops the desk holding that file from enrolling.
    """
    coord = coordinator
    join, token_id = await asyncio.to_thread(
        mint_join_bundle_and_token_id, coord, tmp_path / "join"
    )
    bundle = json.loads(join.read_text(encoding="utf-8"))

    listed = await coord.client.get("/v1/admin/enrollment_tokens", headers=coord.admin_headers())
    assert listed.status_code == 200, listed.text
    assert [row["token_id"] for row in listed.json()] == [token_id]

    revoked = await coord.client.delete(
        f"/v1/admin/enrollment_tokens/{token_id}", headers=coord.admin_headers()
    )
    assert revoked.status_code == 204, revoked.text

    refused = await coord.client.post(
        "/v1/agents/register",
        json=make_register_request(bundle["enrollment_token"]).model_dump(mode="json"),
    )
    assert refused.status_code == 401, refused.text
