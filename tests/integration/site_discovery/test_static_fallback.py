"""Discovery is a fallback: it must not be needed, and it must not cost trust.

ADR 092's availability clause, from both sides. When the configured address is
healthy the agent serves through it and never opens a query at all, so an
explicit address wins over anything multicast could offer. When the address is
gone and the segment answers nothing, the agent says so, keeps the profile it
already had, and its trust material is untouched — which is what the operator
sees when they run doctor afterwards.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from site_mode.site_harness import (
    REPLICA_PORT_COUNT,
    assign_model,
    chat_once,
    create_api_key,
    doctor,
    llama_command,
    mint_join_bundle_via_flw,
    register_chat_model,
    run_site_daemon,
    serve_site_coordinator,
    wait_enrolled,
    wait_replica_ready,
    write_agent_toml,
    write_tls_cert,
)

from site_discovery.discovery_harness import (
    NO_CANDIDATE,
    QUERY_OPENED,
    closed_port,
    origin,
    site_settings,
)

pytestmark = pytest.mark.asyncio

HEALTHY_SITE = "school-pilot-static-healthy"
SILENT_SITE = "school-pilot-silent-segment"

# How long the agent is left running against a coordinator that is gone. The
# probe and the query are both bounded (3s and 2s), so the whole fallback has
# reported its outcome well inside this window.
FALLBACK_WINDOW_S = 12.0


async def test_a_reachable_address_serves_and_opens_no_query(
    site_binary: Path, tmp_path: Path
) -> None:
    """mDNS is configured, the address works, and no query is ever made."""
    coord_dir = tmp_path / "coord"
    coord_dir.mkdir()
    certfile, keyfile = write_tls_cert(coord_dir)
    port = closed_port()
    site = site_settings(
        site_id=HEALTHY_SITE,
        urls=(origin(port),),
        certfile=certfile,
        keyfile=keyfile,
    )
    blob = tmp_path / "model.gguf"
    blob.write_bytes(b"fake-gguf-bytes-for-the-pilot")
    state = tmp_path / "agent-state.json"
    config = tmp_path / "agent.toml"

    async with serve_site_coordinator(
        coord_dir, port=port, certfile=certfile, keyfile=keyfile, site=site
    ) as coord:
        await register_chat_model(coord, blob)
        key = await create_api_key(coord)
        join = await asyncio.to_thread(mint_join_bundle_via_flw, coord, tmp_path / "join")
        assert json.loads(join.read_text(encoding="utf-8"))["mdns_service"] == "_fallow._tcp.local."
        port_start = write_agent_toml(
            config,
            join_bundle=join,
            state_path=state,
            cache_dir=tmp_path / "cache",
            llama_binary=llama_command(),
        )
        async with run_site_daemon(site_binary, config, state) as daemon:
            agent_id = await wait_enrolled(coord)
            await assign_model(coord, [agent_id])
            replica_port = await wait_replica_ready(coord, agent_id)
            upper = port_start + REPLICA_PORT_COUNT
            assert port_start <= replica_port < upper, (
                f"replica port {replica_port} outside the range"
            )
            served = await chat_once(coord, key, echo="static")
            assert served.status_code == 200, daemon.stderr
            rc = await daemon.stop()
        assert rc == 0, daemon.stderr

    log = daemon.stderr
    assert QUERY_OPENED not in log, log
    assert NO_CANDIDATE not in log, log


async def test_a_silent_segment_keeps_the_profile_and_the_pins(
    site_binary: Path, tmp_path: Path
) -> None:
    """Nothing answers, so nothing changes: same profile, same pins, same site.

    The agent enrolls, the coordinator goes away, and the agent restarts into a
    segment with no responder for its site. It reports that discovery produced
    no candidate and keeps its static profile. Afterwards ``doctor`` still reads
    the same site id and still finds the stored pins valid, so an unavailable
    fallback has cost the machine nothing but a bounded wait.
    """
    coord_dir = tmp_path / "coord"
    coord_dir.mkdir()
    certfile, keyfile = write_tls_cert(coord_dir)
    port = closed_port()
    site = site_settings(
        site_id=SILENT_SITE,
        urls=(origin(port),),
        certfile=certfile,
        keyfile=keyfile,
    )
    state = tmp_path / "agent-state.json"
    config = tmp_path / "agent.toml"

    async with serve_site_coordinator(
        coord_dir, port=port, certfile=certfile, keyfile=keyfile, site=site
    ) as coord:
        join = await asyncio.to_thread(mint_join_bundle_via_flw, coord, tmp_path / "join")
        write_agent_toml(
            config,
            join_bundle=join,
            state_path=state,
            cache_dir=tmp_path / "cache",
            llama_binary=llama_command(),
        )
        async with run_site_daemon(site_binary, config, state) as daemon:
            await wait_enrolled(coord)
            rc = await daemon.stop()
        assert rc == 0, daemon.stderr

    enrolled = json.loads(state.read_text(encoding="utf-8"))

    # The coordinator is gone and no responder for this site exists anywhere.
    async with run_site_daemon(site_binary, config, state) as orphan:
        await asyncio.sleep(FALLBACK_WINDOW_S)
        # stderr is read when the daemon is reaped, so stop it before reading it.
        await orphan.stop()
        log = orphan.stderr

    assert QUERY_OPENED in log, log
    assert NO_CANDIDATE in log, log

    stranded = json.loads(state.read_text(encoding="utf-8"))
    assert stranded["site"] == enrolled["site"], "the static profile was rewritten"
    assert stranded["agent_id"] == enrolled["agent_id"]
    assert stranded["device_token"] == enrolled["device_token"]

    report = await doctor(site_binary, config)
    assert report["mode"] == "site", report
    assert report["ok"] is True, report
    assert report["pinned_tls"]["ok"] is True, report
    assert "persisted profile" in report["pinned_tls"]["detail"], report
    assert SILENT_SITE in report["identity"]["detail"], report
