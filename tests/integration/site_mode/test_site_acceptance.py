"""The static Site Mode acceptance suite: prove the real outbound-only vertical.

Every test drives the real components — a pinned-HTTPS coordinator on an exact
loopback address, a join file minted through the ``flw`` CLI code path, the built
Go Site runtime enrolled once against a token-free profile, and a loopback-only
fake llama the supervisor spawns — with no external network. A skipped case is a
failed acceptance run, so the binary is required (the ``site_binary`` fixture
fails loudly when it is absent).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from site_mode.site_harness import (
    SiteCoordinator,
    agent_snapshot,
    assign_model,
    chat_once,
    create_api_key,
    list_agents,
    llama_command,
    mint_join_bundle_via_flw,
    reclaim,
    register_chat_model,
    release,
    run_site_daemon,
    serve_site_coordinator,
    wait_enrolled,
    wait_for,
    wait_replica_ready,
    wait_serving_paused,
    write_agent_toml,
)

pytestmark = pytest.mark.asyncio


class _Serving:
    """A running Site Mode agent under test: daemon, its ids, and its config."""

    def __init__(self, coord, daemon, agent_id, key, config, state, port, join):
        self.coord = coord
        self.daemon = daemon
        self.agent_id = agent_id
        self.key = key
        self.config = config
        self.state = state
        self.port = port
        self.join = join


@contextlib.asynccontextmanager
async def serving_site(
    coord: SiteCoordinator, binary: Path, tmp_path: Path, *, assign: bool = True
) -> AsyncIterator[_Serving]:
    """Mint → enroll → (assign) → READY, yielding a fully wired serving agent."""
    blob = tmp_path / "model.gguf"
    blob.write_bytes(b"fake-gguf-bytes-for-the-pilot")
    await register_chat_model(coord, blob)
    key = await create_api_key(coord)

    join = await asyncio.to_thread(mint_join_bundle_via_flw, coord, tmp_path / "join")
    state = tmp_path / "agent-state.json"
    config = tmp_path / "agent.toml"
    write_agent_toml(
        config,
        join_bundle=join,
        state_path=state,
        cache_dir=tmp_path / "cache",
        llama_binary=llama_command(),
    )
    async with run_site_daemon(binary, config, state) as daemon:
        agent_id = await wait_enrolled(coord)
        port = None
        if assign:
            await assign_model(coord, [agent_id])
            port = await wait_replica_ready(coord, agent_id)
        yield _Serving(coord, daemon, agent_id, key, config, state, port, join)


# ── the full happy-path vertical ─────────────────────────────────────────────


async def test_static_site_vertical_buffered_and_sse(
    coordinator: SiteCoordinator, site_binary: Path, tmp_path: Path
) -> None:
    async with serving_site(coordinator, site_binary, tmp_path) as site:
        daemon = site.daemon

        # Enrolled once and persisted a token-free identity; the join file is gone.
        identity = daemon.identity()
        assert identity["agent_id"] == site.agent_id
        assert identity.get("site") is not None
        assert "enrollment_token" not in json.dumps(identity)
        assert not site.join.exists(), "join file must be removed after enrollment"

        assert 8100 <= site.port < 8108, f"replica port {site.port} outside the range"

        # Buffered JSON through the relay: status, content type, and body preserved.
        buffered = await chat_once(site.coord, site.key, echo="world")
        assert buffered.status_code == 200, daemon.stderr
        assert buffered.headers["content-type"].startswith("application/json")
        assert json.loads(buffered.content)["choices"][0]["message"]["content"] == "world"

        # Raw SSE through the relay: verbatim bytes, streaming media type preserved.
        streamed = await chat_once(site.coord, site.key, echo="Hiya", stream=True)
        assert streamed.status_code == 200, daemon.stderr
        assert streamed.headers["content-type"].startswith("text/event-stream")
        body = streamed.content
        assert b'"content":"Hi"' in body and b'"content":"ya"' in body
        assert body.endswith(b"data: [DONE]\n\n")

        rc = await daemon.stop()
    assert rc == 0, daemon.stderr
    # Resource hygiene: the loopback replica port is released after shutdown.
    assert _port_closed(site.port), "replica listener still open after daemon stop"


async def test_no_retry_after_first_byte_truncates_e2e(
    coordinator: SiteCoordinator, site_binary: Path, tmp_path: Path
) -> None:
    """Once a response byte has crossed the relay, a mid-stream drop truncates.

    The fake llama emits one partial SSE event then drops the connection. The
    client keeps the bytes it already received with a 200 that was already
    committed, and the stream ends without a terminator — no replay, no retry
    after the first byte. (The pre-first-byte repick and generation-invalidation
    boundaries are proven against the same real RelayBroker and SiteAwareTransport
    in packages/fallow-coordinator/tests/gateway/test_site_transport.py.)
    """
    async with serving_site(coordinator, site_binary, tmp_path) as site:
        resp = await site.coord.client.post(
            "/v1/chat/completions",
            json={
                "model": "qwen2.5-7b",
                "stream": True,
                "_echo": "Hello",
                "_fake_mode": "truncate",
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers={"Authorization": f"Bearer {site.key}"},
        )
        assert resp.status_code == 200, site.daemon.stderr
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = resp.content
        # The first (large) SSE event crossed the relay; the stream is truncated
        # cleanly with no terminator and, crucially, no replay/retry after it.
        assert body.startswith(b"data: ")
        assert b'"content":"' + b"x" * 16000 in body
        assert b"[DONE]" not in body
        rc = await site.daemon.stop()
    assert rc == 0, site.daemon.stderr


def _port_closed(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) != 0


# ── reclaim / release: immediate suspension and durable-generation resume ────


async def test_reclaim_suspends_then_release_resumes(
    coordinator: SiteCoordinator, site_binary: Path, tmp_path: Path
) -> None:
    async with serving_site(coordinator, site_binary, tmp_path) as site:
        # Serving before reclaim.
        first = await chat_once(site.coord, site.key, echo="before")
        assert first.status_code == 200, site.daemon.stderr

        # Reclaim: the user takes the machine. The replica suspends and the agent
        # leaves routing without waiting for the next heartbeat window.
        await reclaim(site_binary, site.config)
        await wait_serving_paused(site.coord, site.agent_id, True)
        await wait_for(
            lambda: _no_ready_replica(site.coord, site.agent_id),
            timeout=15.0,
            what="replica suspended after reclaim",
        )

        # Release: normal idle-based serving resumes and the durable presence
        # generation advances to match the fence, so relayed serving works again.
        await release(site_binary, site.config)
        await wait_serving_paused(site.coord, site.agent_id, False)
        await wait_replica_ready(site.coord, site.agent_id)

        resumed = await wait_for(
            lambda: _served(site.coord, site.key),
            timeout=20.0,
            what="serving resumes after release",
        )
        assert resumed == "after-release"

        rc = await site.daemon.stop()
    assert rc == 0, site.daemon.stderr


async def _no_ready_replica(coord: SiteCoordinator, agent_id: str) -> bool:
    snap = await agent_snapshot(coord, agent_id)
    if snap is None:
        return False
    return not any(r.get("state") == "ready" for r in snap.get("replicas", ()))


async def _served(coord: SiteCoordinator, key: str, echo: str = "after-release") -> str | None:
    resp = await chat_once(coord, key, echo=echo)
    if resp.status_code != 200:
        return None
    return json.loads(resp.content)["choices"][0]["message"]["content"]


# ── restart: monotonic sequencing + token-free resume without re-enrollment ──


async def test_agent_restart_resumes_without_reenrollment(
    coordinator: SiteCoordinator, site_binary: Path, tmp_path: Path
) -> None:
    async with serving_site(coordinator, site_binary, tmp_path) as site:
        assert await chat_once(site.coord, site.key) is not None
        first_id = site.agent_id
        # Let the shared sequence advance past enrollment via a few heartbeats.
        await asyncio.sleep(1.0)
        rc = await site.daemon.stop()
        assert rc == 0, site.daemon.stderr
        seq_before = int(site.daemon.identity().get("seq", 0))
        config, state = site.config, site.state

    # The join file is already consumed; restart reads the persisted profile only.
    assert not site.join.exists()
    async with run_site_daemon(site_binary, config, state) as daemon2:
        # No second enrollment: the same single agent id, no new registration.
        await wait_for(
            lambda: _single_agent_is(site.coord, first_id),
            timeout=15.0,
            what="restart resumes the same agent without re-enrolling",
        )
        agents = await list_agents(site.coord)
        assert len(agents) == 1 and agents[0]["agent_id"] == first_id

        await wait_replica_ready(site.coord, first_id)
        # Regression for the fence/durable-generation consistency fix: a graceful
        # shutdown that fenced in-flight work must not strand a same-identity
        # restart. Serving resumes with no reclaim/release or other presence cycle.
        served = await wait_for(
            lambda: _served(site.coord, site.key, "resumed"),
            timeout=25.0,
            what="relayed serving resumes after graceful shutdown + restart",
        )
        assert served == "resumed"

        # The restart-safe sequence never regresses below the persisted high-water.
        await asyncio.sleep(1.0)
        rc2 = await daemon2.stop()
        assert rc2 == 0, daemon2.stderr
        seq_after = int(daemon2.identity().get("seq", 0))
    assert seq_after >= seq_before, f"sequence regressed: {seq_after} < {seq_before}"
    assert daemon2.identity()["agent_id"] == first_id
    assert daemon2.identity().get("device_token"), "token-free profile lost its device token"


async def _single_agent_is(coord: SiteCoordinator, agent_id: str) -> bool:
    agents = await list_agents(coord)
    return len(agents) == 1 and agents[0]["agent_id"] == agent_id


# ── coordinator restart: drop claims, resume held polling, serve after reconnect


async def test_coordinator_restart_resumes_held_polling(site_binary: Path, tmp_path: Path) -> None:
    """The coordinator restarts on the same origin while the agent keeps running.

    Dropped in-memory relay claims must not permanently stop serving: the
    supervised claim runner reconnects and the same agent resumes held claim
    polling, serving a relayed request after the coordinator returns. The daemon
    is managed independently of the coordinator so it outlives the restart.
    """
    coord_dir = tmp_path / "coord"
    coord_dir.mkdir()
    blob = tmp_path / "model.gguf"
    blob.write_bytes(b"fake-gguf-bytes-for-the-pilot")

    coord1_cm = serve_site_coordinator(coord_dir)
    coord1 = await coord1_cm.__aenter__()
    port, certfile, keyfile = coord1.port, coord1.certfile, coord1.keyfile
    await register_chat_model(coord1, blob)
    key = await create_api_key(coord1)
    join = await asyncio.to_thread(mint_join_bundle_via_flw, coord1, tmp_path / "join")
    state = tmp_path / "agent-state.json"
    config = tmp_path / "agent.toml"
    write_agent_toml(
        config,
        join_bundle=join,
        state_path=state,
        cache_dir=tmp_path / "cache",
        llama_binary=llama_command(),
    )

    daemon_cm = run_site_daemon(site_binary, config, state)
    daemon = await daemon_cm.__aenter__()
    try:
        agent_id = await wait_enrolled(coord1)
        await assign_model(coord1, [agent_id])
        await wait_replica_ready(coord1, agent_id)
        assert (await chat_once(coord1, key, echo="pre")).status_code == 200, daemon.stderr

        # Tear the coordinator down (dropping its in-memory relay) while the daemon
        # keeps running, then bring the same origin back up on the same db.
        await coord1_cm.__aexit__(None, None, None)
        await asyncio.sleep(1.0)

        async with serve_site_coordinator(
            coord_dir, port=port, certfile=certfile, keyfile=keyfile
        ) as coord2:
            agents = await list_agents(coord2)
            assert len(agents) == 1 and agents[0]["agent_id"] == agent_id
            await wait_replica_ready(coord2, agent_id)
            served = await wait_for(
                lambda: _served(coord2, key, "post-restart"),
                timeout=30.0,
                what="agent resumes held polling and serves after coordinator restart",
            )
            assert served == "post-restart"

        rc = await daemon.stop()
        assert rc == 0, daemon.stderr
    finally:
        with contextlib.suppress(Exception):
            await daemon_cm.__aexit__(None, None, None)
