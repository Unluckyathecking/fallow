"""The coordinator moves; the agent comes back to it without re-enrolling.

ADR 092's first clause: with the site id and the pinned key unchanged, an agent
whose coordinator has taken a new address recovers it and resumes serving, and
it does so on the identity it already holds — no second registration and no new
join token. The second test is the control: an agent that did not opt into
discovery keeps its old behaviour exactly, taking its first configured origin
with no probe of any kind, so opting in is what buys the recovery.
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
    list_agents,
    llama_command,
    mint_join_bundle_via_flw,
    register_chat_model,
    run_site_daemon,
    serve_site_coordinator,
    wait_enrolled,
    wait_for,
    wait_process_exit,
    wait_replica_ready,
    write_agent_toml,
    write_tls_cert,
)

from site_discovery.discovery_harness import (
    ORIGIN_UNUSABLE,
    QUERY_OPENED,
    closed_port,
    origin,
    site_settings,
)

pytestmark = pytest.mark.asyncio

# A site id unique to this module, so a query that does reach a real segment can
# never match a coordinator belonging to somebody else's run.
MOVED_SITE = "school-pilot-moved"
STATIC_ONLY_SITE = "school-pilot-static-only"


async def _served(coord: SiteCoordinator, key: str, echo: str) -> str | None:
    resp = await chat_once(coord, key, echo=echo)
    if resp.status_code != 200:
        return None
    content: str = json.loads(resp.content)["choices"][0]["message"]["content"]
    return content


async def test_moved_coordinator_is_recovered_without_re_enrolment(
    site_binary: Path, tmp_path: Path
) -> None:
    """Same site id, same pinned key, new address: the agent reconnects as itself.

    The coordinator publishes the address it is on and the address it will move
    to. The agent enrolls, serves, and is stopped; the coordinator restarts on
    the second address with the same certificate and the same database. On its
    next start the agent finds its first origin dead, reaches the second one
    through the same pinned client, and resumes as the agent it already was.
    """
    coord_dir = tmp_path / "coord"
    coord_dir.mkdir()
    certfile, keyfile = write_tls_cert(coord_dir)
    first_port, moved_port = closed_port(), closed_port()
    site = site_settings(
        site_id=MOVED_SITE,
        urls=(origin(first_port), origin(moved_port)),
        certfile=certfile,
        keyfile=keyfile,
    )
    blob = tmp_path / "model.gguf"
    blob.write_bytes(b"fake-gguf-bytes-for-the-pilot")
    state = tmp_path / "agent-state.json"
    config = tmp_path / "agent.toml"

    async with serve_site_coordinator(
        coord_dir, port=first_port, certfile=certfile, keyfile=keyfile, site=site
    ) as before:
        await register_chat_model(before, blob)
        key = await create_api_key(before)
        join = await asyncio.to_thread(mint_join_bundle_via_flw, before, tmp_path / "join")
        write_agent_toml(
            config,
            join_bundle=join,
            state_path=state,
            cache_dir=tmp_path / "cache",
            llama_binary=llama_command(),
            port_start=8400,
        )
        async with run_site_daemon(site_binary, config, state) as daemon:
            agent_id = await wait_enrolled(before)
            await assign_model(before, [agent_id])
            await wait_replica_ready(before, agent_id)
            assert await _served(before, key, "before-move") == "before-move", daemon.stderr
            rc = await daemon.stop()
        assert rc == 0, daemon.stderr

    identity_before = json.loads(state.read_text(encoding="utf-8"))
    assert not join.exists(), "the join token was consumed by the first enrollment"

    async with serve_site_coordinator(
        coord_dir, port=moved_port, certfile=certfile, keyfile=keyfile, site=site
    ) as moved:
        async with run_site_daemon(site_binary, config, state) as resumed:
            await wait_for(
                lambda: _only_agent(moved, agent_id),
                timeout=30.0,
                what="the same agent reconnects at the moved coordinator",
            )
            replica_port = await wait_replica_ready(moved, agent_id)
            assert 8400 <= replica_port < 8408, f"replica port {replica_port} outside the range"
            served = await wait_for(
                lambda: _served(moved, key, "after-move"),
                timeout=30.0,
                what="relayed serving resumes at the moved coordinator",
            )
            assert served == "after-move"
            rc = await resumed.stop()
        assert rc == 0, resumed.stderr

    # It recovered by trying its configured origins in order, not by querying:
    # a reachable candidate in the profile is answer enough.
    log = resumed.stderr
    assert ORIGIN_UNUSABLE in log, log
    assert QUERY_OPENED not in log, log

    # Same identity, same trust: nothing about the profile was relearned.
    identity_after = json.loads(state.read_text(encoding="utf-8"))
    assert identity_after["agent_id"] == identity_before["agent_id"]
    assert identity_after["device_token"] == identity_before["device_token"]
    assert identity_after["site"] == identity_before["site"]
    assert not join.exists(), "no new join token was minted or consumed"


async def _only_agent(coord: SiteCoordinator, agent_id: str) -> bool:
    agents = await list_agents(coord)
    return len(agents) == 1 and agents[0]["agent_id"] == agent_id


async def test_a_profile_without_mdns_probes_nothing_and_stays_put(
    site_binary: Path, tmp_path: Path
) -> None:
    """The control: no mdns_service, no probe, no query, no recovery.

    The same two-address profile without the mDNS opt-in takes its first origin
    and dials it, exactly as an agent behaved before discovery existed. The
    coordinator is live on the second address and is never contacted, the
    enrollment fails, and the join token is left unconsumed for the operator.
    """
    coord_dir = tmp_path / "coord"
    coord_dir.mkdir()
    certfile, keyfile = write_tls_cert(coord_dir)
    dead_port, live_port = closed_port(), closed_port()
    site = site_settings(
        site_id=STATIC_ONLY_SITE,
        urls=(origin(dead_port), origin(live_port)),
        certfile=certfile,
        keyfile=keyfile,
        mdns=False,
    )
    state = tmp_path / "agent-state.json"
    config = tmp_path / "agent.toml"

    async with serve_site_coordinator(
        coord_dir, port=live_port, certfile=certfile, keyfile=keyfile, site=site
    ) as coord:
        join = await asyncio.to_thread(mint_join_bundle_via_flw, coord, tmp_path / "join")
        assert json.loads(join.read_text(encoding="utf-8"))["mdns_service"] is None
        write_agent_toml(
            config,
            join_bundle=join,
            state_path=state,
            cache_dir=tmp_path / "cache",
            llama_binary=llama_command(),
            port_start=8420,
        )
        async with run_site_daemon(site_binary, config, state) as daemon:
            rc = await wait_process_exit(daemon, timeout=30.0)
        assert rc != 0, "enrollment against a dead origin must fail loudly"

        log = daemon.stderr
        assert ORIGIN_UNUSABLE not in log, log
        assert QUERY_OPENED not in log, log
        assert await list_agents(coord) == [], "the live coordinator was contacted"

    assert not state.exists(), "a failed enrollment must not persist an identity"
    assert join.exists(), "an unused join token must survive a failed enrollment"
