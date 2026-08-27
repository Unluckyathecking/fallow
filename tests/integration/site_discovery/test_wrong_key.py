"""A responder holding a site address without the pinned key gets no request.

ADR 092's trust clause. Discovery and static configuration both hand the agent
nothing but an address, and the pinned client is what decides whether an address
belongs to the coordinator. A responder that answers on that address with a
different key is therefore skipped before any request is written to it, and the
stored pin set is left exactly as it was.
"""

from __future__ import annotations

import asyncio
import json
import ssl
from pathlib import Path

import httpx
import pytest
from site_mode.site_harness import (
    list_agents,
    llama_command,
    mint_join_bundle_via_flw,
    reserve_loopback_sockets,
    run_site_daemon,
    serve_site_coordinator,
    wait_enrolled,
    wait_local_identity,
    write_agent_toml,
    write_tls_cert,
)

from site_discovery.discovery_harness import (
    ORIGIN_UNUSABLE,
    closed_port,
    origin,
    serve_wrong_key_responder,
    site_settings,
)

pytestmark = pytest.mark.asyncio

WRONG_KEY_SITE = "school-pilot-wrong-key"


async def test_a_wrong_key_responder_receives_no_request(site_binary: Path, tmp_path: Path) -> None:
    """The imposter answers the test and never hears from the agent.

    The profile lists the imposter's origin first and the coordinator's second.
    A liveness request proves the imposter is really serving, so the silence that
    follows is the pin's decision and not an unreachable host. The agent then
    enrolls at the coordinator, and its persisted pin set is byte-for-byte the
    one the join bundle carried.
    """
    coord_dir = tmp_path / "coord"
    coord_dir.mkdir()
    certfile, keyfile = write_tls_cert(coord_dir)
    # Hold the imposter's port so its origin can be published before it is served.
    held, imposter_port = reserve_loopback_sockets()
    coord_port = closed_port()
    site = site_settings(
        site_id=WRONG_KEY_SITE,
        urls=(origin(imposter_port), origin(coord_port)),
        certfile=certfile,
        keyfile=keyfile,
    )
    state = tmp_path / "agent-state.json"
    config = tmp_path / "agent.toml"

    async with serve_wrong_key_responder(tmp_path / "wrong-key", held, imposter_port) as imposter:
        # The imposter serves a valid certificate that simply is not the pinned
        # one, so a client that trusts its leaf gets a real answer from it.
        verify = ssl.create_default_context(cafile=str(imposter.certfile))
        async with httpx.AsyncClient(verify=verify, trust_env=False, timeout=10.0) as probe:
            assert (await probe.get(imposter.origin + "/liveness")).status_code == 204
        assert imposter.requests == ["/liveness"]
        imposter.requests.clear()

        async with serve_site_coordinator(
            coord_dir, port=coord_port, certfile=certfile, keyfile=keyfile, site=site
        ) as coord:
            join = await asyncio.to_thread(mint_join_bundle_via_flw, coord, tmp_path / "join")
            bundle = json.loads(join.read_text(encoding="utf-8"))
            write_agent_toml(
                config,
                join_bundle=join,
                state_path=state,
                cache_dir=tmp_path / "cache",
                llama_binary=llama_command(),
            )
            async with run_site_daemon(site_binary, config, state) as daemon:
                agent_id = await wait_enrolled(coord)
                # The coordinator commits the agent row before it finishes
                # writing the register response; stopping in that gap cancels a
                # read the daemon is still inside. Wait for the identity it
                # persists afterwards, so a clean shutdown cannot read as a fault.
                await wait_local_identity(daemon)
                rc = await daemon.stop()
            assert rc == 0, daemon.stderr

            agents = await list_agents(coord)
            assert len(agents) == 1 and agents[0]["agent_id"] == agent_id

    assert not imposter.requests, f"a request reached the wrong-key responder: {imposter.requests}"
    # It did try the imposter's origin first and rejected it there.
    assert ORIGIN_UNUSABLE in daemon.stderr, daemon.stderr

    identity = json.loads(state.read_text(encoding="utf-8"))
    assert identity["site"]["coordinator_spki_sha256"] == bundle["coordinator_spki_sha256"]
    assert identity["site"]["site_id"] == WRONG_KEY_SITE
    assert identity["site"]["coordinator_urls"] == bundle["coordinator_urls"]
