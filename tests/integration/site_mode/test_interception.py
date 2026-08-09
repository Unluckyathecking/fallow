"""Site Mode against a TLS-inspecting middlebox: refuse, name it, recover.

The pilot site inspects HTTPS, so these cases put a loopback TLS terminator on
the coordinator's exact origin — the coordinator's hostname under a different key
— and drive the real Go Site runtime at it. They prove the agent refuses the
intercepted origin before a request byte or a credential is written, never
downgrades to cleartext or a proxy, names a pin mismatch apart from an
unreachable coordinator, and resumes claims on untouched enrollment state once
the interceptor is gone and the real coordinator returns.

Everything below rides the same harness as the rest of the suite: the real
coordinator, a join file minted through the ``flw`` code path, and the built Go
binary. A skipped case is a failed acceptance run, so nothing here skips.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import ssl
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from site_mode.interceptor import cleartext_sink, intercept_origin
from site_mode.site_harness import (
    LOOPBACK,
    SITE_HOST,
    SiteCoordinator,
    assign_model,
    chat_once,
    create_api_key,
    list_agents,
    llama_command,
    mint_join_bundle_via_flw,
    register_chat_model,
    reserve_loopback_sockets,
    run_site_daemon,
    serve_site_coordinator,
    wait_enrolled,
    wait_for,
    wait_process_exit,
    wait_replica_ready,
    write_agent_toml,
)

pytestmark = pytest.mark.asyncio

# Every proxy variable the Go client could honour. Pointed at the cleartext sink,
# they turn any dial around the pinned transport into a recorded connection.
_PROXY_VARS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)

# A replica port range of this file's own, so an interception scenario can run
# beside the rest of the suite without contending for the same loopback ports.
_PORT_START = 8300


@dataclass
class _Origin:
    """A coordinator origin whose port is free, with join files already minted."""

    dir: Path
    port: int
    certfile: Path
    keyfile: Path
    joins: list[Path]


@contextlib.asynccontextmanager
async def _vacated_origin(tmp_path: Path, *, count: int = 1) -> AsyncIterator[_Origin]:
    """Mint ``count`` join files from a real coordinator, then free its origin.

    The join files carry the coordinator's true pin and its exact loopback origin.
    Stopping the coordinator before yielding hands that origin to the interceptor,
    which is what a middlebox does to an agent: same address, same name, wrong key.
    """
    coord_dir = tmp_path / "coord"
    coord_dir.mkdir()
    async with serve_site_coordinator(coord_dir) as coord:
        blob = tmp_path / "model.gguf"
        blob.write_bytes(b"fake-gguf-bytes-for-the-pilot")
        await register_chat_model(coord, blob)
        joins = [
            await asyncio.to_thread(mint_join_bundle_via_flw, coord, tmp_path / f"join-{i}")
            for i in range(count)
        ]
        origin = _Origin(coord_dir, coord.port, coord.certfile, coord.keyfile, joins)
    yield origin


async def _enrollment_attempt(
    site_binary: Path, workdir: Path, join: Path
) -> tuple[int, str, Path]:
    """Run one Site Mode daemon to its own exit; return (rc, stderr, state path)."""
    workdir.mkdir(parents=True, exist_ok=True)
    state = workdir / "agent-state.json"
    config = workdir / "agent.toml"
    write_agent_toml(
        config,
        join_bundle=join,
        state_path=state,
        cache_dir=workdir / "cache",
        llama_binary=llama_command(),
        port_start=_PORT_START,
    )
    async with run_site_daemon(site_binary, config, state) as daemon:
        rc = await wait_process_exit(daemon, timeout=20.0)
    return rc, daemon.stderr, state


# ── first contact: the agent refuses before it writes anything ────────────────


async def test_interception_writes_no_request_bytes_and_no_credential(
    site_binary: Path, tmp_path: Path
) -> None:
    """An intercepted enrollment fails closed: a handshake, then nothing."""
    async with _vacated_origin(tmp_path) as origin:
        join = origin.joins[0]
        token = json.loads(join.read_text())["enrollment_token"]
        state = tmp_path / "run" / "agent-state.json"
        config = tmp_path / "run" / "agent.toml"
        config.parent.mkdir()
        write_agent_toml(
            config,
            join_bundle=join,
            state_path=state,
            cache_dir=tmp_path / "cache",
            llama_binary=llama_command(),
            port_start=_PORT_START,
        )

        with (
            cleartext_sink() as sink,
            intercept_origin(origin.port, tmp_path / "mitm") as mitm,
        ):
            proxied = dict.fromkeys(_PROXY_VARS, sink.url)
            async with run_site_daemon(site_binary, config, state, env=proxied) as daemon:
                rc = await wait_process_exit(daemon, timeout=20.0)

            assert rc != 0, daemon.stderr
            assert "pin mismatch" in daemon.stderr.lower(), daemon.stderr

            # The agent reached the intercepted origin, spoke TLS, and stopped
            # there: no request line, no bearer, and not one byte of the token.
            assert mitm.connections, "the agent never dialled the intercepted origin"
            assert [c.kind for c in mitm.connections] == ["tls"] * len(mitm.connections), (
                f"non-TLS traffic reached the intercepted origin: {mitm.connections}"
            )
            assert mitm.payload == b"", f"the agent wrote request bytes: {mitm.payload!r}"
            assert token.encode() not in mitm.payload
            assert b"Authorization" not in mitm.payload

            # And it did not dial around the pin: the poisoned proxy saw nothing.
            assert sink.connections == [], f"the agent fell back to a proxy: {sink.connections}"

        # Nothing was persisted and the one-time token was not consumed.
        assert not state.exists(), "a refused enrollment must persist no identity"
        assert join.exists() and json.loads(join.read_text())["enrollment_token"] == token

        # The real coordinator, brought back on its own origin, never saw a request.
        async with serve_site_coordinator(
            origin.dir, port=origin.port, certfile=origin.certfile, keyfile=origin.keyfile
        ) as coord:
            assert await list_agents(coord) == []


# ── a pin mismatch is its own diagnosis, not "the coordinator is down" ────────


async def test_pin_mismatch_reads_apart_from_an_unreachable_coordinator(
    site_binary: Path, tmp_path: Path
) -> None:
    """The same origin, interception versus silence, must not report the same fault."""
    async with _vacated_origin(tmp_path, count=2) as origin:
        with intercept_origin(origin.port, tmp_path / "mitm"):
            intercepted_rc, intercepted, _ = await _enrollment_attempt(
                site_binary, tmp_path / "intercepted", origin.joins[0]
            )
        # The interceptor is gone and nothing replaces it, so the same origin is
        # now simply unreachable; only the listener differs between the two runs.
        unreachable_rc, unreachable, _ = await _enrollment_attempt(
            site_binary, tmp_path / "unreachable", origin.joins[1]
        )

    assert intercepted_rc != 0 and unreachable_rc != 0
    assert "pin mismatch" in intercepted.lower(), intercepted
    assert "pin" not in unreachable.lower(), unreachable
    assert "connect" in unreachable.lower() or "refused" in unreachable.lower(), unreachable


# ── the listeners can hear: an empty recording means silence, not deafness ────


async def test_the_listeners_record_a_client_that_does_not_check_the_pin(
    tmp_path: Path,
) -> None:
    """Every case above rests on an empty recording, so prove a full one is possible.

    A client that skips the pin check hands the middlebox its request line and its
    bearer. Recording that here is what makes the agent's silence a finding.
    """
    socks, port = reserve_loopback_sockets()
    for sock in socks:
        sock.close()
    request = b"POST /v1/agents/register HTTP/1.1\r\nAuthorization: Bearer leak\r\n\r\n"

    with intercept_origin(port, tmp_path / "mitm") as mitm, cleartext_sink() as sink:
        await asyncio.to_thread(_send_over_tls, port, request)
        await asyncio.to_thread(_send_cleartext, sink.port, request)
        await wait_for(
            lambda: mitm.connections and sink.connections,
            timeout=15.0,
            what="both listeners to record the trusting client",
        )
        assert mitm.payload == request and [c.kind for c in mitm.connections] == ["tls"]
        assert sink.payload == request and [c.kind for c in sink.connections] == ["cleartext"]


def _send_over_tls(port: int, request: bytes) -> None:
    """Speak TLS to the interceptor without verifying anything, then send bytes."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with (
        socket.create_connection((LOOPBACK, port), timeout=10.0) as raw,
        context.wrap_socket(raw, server_hostname=SITE_HOST) as tls,
    ):
        tls.sendall(request)


def _send_cleartext(port: int, request: bytes) -> None:
    with socket.create_connection((LOOPBACK, port), timeout=10.0) as raw:
        raw.sendall(request)


# ── interception mid-life: state survives it and claims resume after it ───────


async def test_interception_leaves_enrollment_intact_and_claims_resume(
    site_binary: Path, tmp_path: Path
) -> None:
    """A middlebox appears in front of a serving agent, then goes away again.

    The agent is enrolled and relaying before the interceptor takes the origin.
    Through the interception window it must write nothing to the middlebox and
    keep its enrollment; once the real coordinator returns it must resume claims
    on the same identity, with no second registration.
    """
    coord_dir = tmp_path / "coord"
    coord_dir.mkdir()
    blob = tmp_path / "model.gguf"
    blob.write_bytes(b"fake-gguf-bytes-for-the-pilot")

    coord_cm = serve_site_coordinator(coord_dir)
    coord = await coord_cm.__aenter__()
    port, certfile, keyfile = coord.port, coord.certfile, coord.keyfile
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
        port_start=_PORT_START,
    )

    daemon_cm = run_site_daemon(site_binary, config, state)
    daemon = await daemon_cm.__aenter__()
    try:
        agent_id = await wait_enrolled(coord)
        await assign_model(coord, [agent_id])
        await wait_replica_ready(coord, agent_id)
        assert (await chat_once(coord, key, echo="pre")).status_code == 200, daemon.stderr
        enrolled = _enrollment_state(state)

        # The middlebox takes the coordinator's origin while the agent keeps running.
        await coord_cm.__aexit__(None, None, None)
        with intercept_origin(port, tmp_path / "mitm") as mitm:
            await wait_for(
                lambda: len(mitm.connections) >= 2,
                timeout=30.0,
                what="the agent to redial the intercepted origin",
            )
            assert [c.kind for c in mitm.connections] == ["tls"] * len(mitm.connections), (
                f"non-TLS traffic reached the intercepted origin: {mitm.connections}"
            )
            assert mitm.payload == b"", f"the agent wrote request bytes: {mitm.payload!r}"
            # Enrollment is not re-negotiated or discarded under interception.
            assert _enrollment_state(state) == enrolled

        # The real coordinator returns on its own origin.
        async with serve_site_coordinator(
            coord_dir, port=port, certfile=certfile, keyfile=keyfile
        ) as restored:
            agents = await list_agents(restored)
            assert len(agents) == 1 and agents[0]["agent_id"] == agent_id, "agent re-enrolled"
            await wait_replica_ready(restored, agent_id)
            served = await wait_for(
                lambda: _served(restored, key),
                timeout=40.0,
                what="claims to resume once the interceptor is gone",
            )
            assert served == "post-interception"

        rc = await daemon.stop()
        assert rc == 0, daemon.stderr
        assert "pin mismatch" in daemon.stderr.lower(), daemon.stderr
        assert _enrollment_state(state) == enrolled
    finally:
        with contextlib.suppress(Exception):
            await daemon_cm.__aexit__(None, None, None)


def _enrollment_state(state: Path) -> dict:
    """The identity that enrollment established, without the moving sequence."""
    identity = json.loads(state.read_text(encoding="utf-8"))
    return {k: identity.get(k) for k in ("agent_id", "device_token", "site")}


async def _served(coord: SiteCoordinator, key: str) -> str | None:
    resp = await chat_once(coord, key, echo="post-interception")
    if resp.status_code != 200:
        return None
    return str(json.loads(resp.content)["choices"][0]["message"]["content"])
