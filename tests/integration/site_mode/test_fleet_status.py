"""The fleet status route against the real Site Mode vertical.

The unit tests prove the route's shape; this proves it tells the truth. One real
Go Site agent enrolls through a minted join file, serves one relayed request and
then has its machine reclaimed, and the route is read at each step: enrollment
mode and transport as persisted, presence as the agent reports it, and the claim
outcome the broker actually recorded.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from site_mode.site_harness import (
    SiteCoordinator,
    assign_model,
    chat_once,
    create_api_key,
    llama_command,
    mint_join_bundle_via_flw,
    reclaim,
    register_chat_model,
    release,
    run_site_daemon,
    wait_enrolled,
    wait_for,
    wait_replica_ready,
    wait_serving_paused,
    write_agent_toml,
)

pytestmark = pytest.mark.asyncio


async def fleet_status(coord: SiteCoordinator) -> list[dict]:
    response = await coord.client.get("/v1/admin/site/status", headers=coord.admin_headers())
    assert response.status_code == 200, response.text
    return list(response.json()["agents"])


async def only_row(coord: SiteCoordinator, agent_id: str) -> dict:
    rows = await fleet_status(coord)
    assert [row["agent_id"] for row in rows] == [agent_id]
    return rows[0]


async def test_status_reports_the_live_harness_agent(
    coordinator: SiteCoordinator, site_binary: Path, tmp_path: Path
) -> None:
    coord = coordinator
    blob = tmp_path / "model.gguf"
    blob.write_bytes(b"fake-gguf-bytes-for-the-pilot")
    await register_chat_model(coord, blob)
    key = await create_api_key(coord)

    join = await asyncio.to_thread(mint_join_bundle_via_flw, coord, tmp_path / "join")
    # Read now: enrolling consumes the join file, and its token is what the
    # status view must never echo.
    minted = json.loads(join.read_text(encoding="utf-8"))
    config = tmp_path / "agent.toml"
    state = tmp_path / "agent-state.json"
    write_agent_toml(
        config,
        join_bundle=join,
        state_path=state,
        cache_dir=tmp_path / "cache",
        llama_binary=llama_command(),
    )

    async with run_site_daemon(site_binary, config, state):
        agent_id = await wait_enrolled(coord)

        # Enrolled through a join file: site enrollment, relay transport, and no
        # claim has ever terminated yet.
        row = await only_row(coord, agent_id)
        assert row["enrollment_mode"] == "site"
        assert row["transport"] == "site_relay"
        assert row["last_claim"] == "none"
        assert row["last_claim_code"] is None
        assert row["heartbeat_age_s"] < 30.0

        await assign_model(coord, [agent_id])
        await wait_replica_ready(coord, agent_id)

        row = await wait_for(
            lambda: _ready_and_available(coord, agent_id),
            timeout=20.0,
            what="the status route to report a ready, available agent",
        )
        assert row["presence_state"] == "idle"
        idle_generation = row["presence_generation"]

        response = await chat_once(coord, key)
        assert response.status_code == 200, response.text

        row = await wait_for(
            lambda: _claim_settled(coord, agent_id),
            timeout=20.0,
            what="the status route to report the served claim",
        )
        assert row["last_claim"] == "finished"
        assert row["last_claim_code"] is None

        await reclaim(site_binary, config)
        await wait_serving_paused(coord, agent_id, True)

        row = await only_row(coord, agent_id)
        assert row["presence_state"] == "reclaimed"
        assert row["available"] is False
        assert row["presence_generation"] > idle_generation
        assert row["last_claim"] == "finished", "reclaim does not rewrite a settled claim"

        # No token, pin or join material anywhere in the view.
        body = json.dumps(await fleet_status(coord))
        assert coord.config.admin_key not in body
        assert minted["enrollment_token"] not in body
        for pin in minted["coordinator_spki_sha256"]:
            assert pin not in body
        assert "sha256/" not in body and "token" not in body

        # Hand the machine back before teardown: a daemon stopped while paused
        # leaves its suspended replica holding the loopback port.
        await release(site_binary, config)
        await wait_serving_paused(coord, agent_id, False)


async def _ready_and_available(coord: SiteCoordinator, agent_id: str) -> dict | None:
    row = await only_row(coord, agent_id)
    return row if row["available"] and row["ready_replicas"] >= 1 else None


async def _claim_settled(coord: SiteCoordinator, agent_id: str) -> dict | None:
    row = await only_row(coord, agent_id)
    return row if row["last_claim"] != "none" else None
