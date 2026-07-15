"""Scenario 5 — gateway E2E against a real loopback replica stub.

A registered CHAT model is served by a READY replica whose host:port (loopback,
ephemeral) is advertised through a real heartbeat. A keyed client then:
  * streams ``/v1/chat/completions`` and receives verbatim SSE bytes;
  * gets a clean truncation when the stub dies mid-stream; and
  * is served after a pre-first-byte retry when the first (dead) replica refuses.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
from integration_helpers import (
    CHAT_MODEL,
    Harness,
    HarnessFactory,
    bearer,
    create_api_key,
    enroll_agent,
    heartbeat,
    make_manifest,
    make_replica,
    mint_enrollment_token,
    register_model,
)
from stub_server import StubServer, reserve_ordered_ports

from fallow_protocol.capabilities import WorkerKind
from fallow_protocol.models import ReplicaState

_SSE = (
    b'data: {"choices":[{"delta":{"content":"He"}}]}\n\n',
    b'data: {"choices":[{"delta":{"content":"llo"}}]}\n\n',
    b"data: [DONE]\n\n",
)
_PARTIAL = b'data: {"choices":[{"delta":{"content":"He'  # an SSE event cut off mid-line


async def _setup(raw: httpx.AsyncClient, tmp_path: Path) -> str:
    """Register the CHAT model + an all-models api key; return the key."""
    blob = tmp_path / "chat.gguf"
    blob.write_bytes(b"fake-gguf")
    await register_model(raw, make_manifest(CHAT_MODEL, WorkerKind.CHAT), str(blob))
    return await create_api_key(raw, "team-a")


async def test_streaming_bytes_arrive_verbatim(
    make_harness: HarnessFactory, tmp_path: Path
) -> None:
    harness: Harness = await make_harness()
    raw = harness.client
    key = await _setup(raw, tmp_path)
    agent = await enroll_agent(raw, await mint_enrollment_token(raw))

    async with StubServer(chunks=_SSE, content_type="text/event-stream") as stub:
        await heartbeat(
            agent, replicas=(make_replica(CHAT_MODEL, port=stub.port, state=ReplicaState.READY),)
        )
        resp = await raw.post(
            "/v1/chat/completions",
            json={
                "model": CHAT_MODEL,
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
            headers=bearer(key),
        )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.content == b"".join(_SSE)  # byte-for-byte incl the [DONE] terminator


async def test_midstream_kill_truncates_cleanly(
    make_harness: HarnessFactory, tmp_path: Path
) -> None:
    harness: Harness = await make_harness()
    raw = harness.client
    key = await _setup(raw, tmp_path)
    agent = await enroll_agent(raw, await mint_enrollment_token(raw))

    # The stub sends one partial SSE event, then closes the connection (EOF).
    async with StubServer(chunks=(_PARTIAL,), content_type="text/event-stream") as stub:
        await heartbeat(
            agent, replicas=(make_replica(CHAT_MODEL, port=stub.port, state=ReplicaState.READY),)
        )
        resp = await raw.post(
            "/v1/chat/completions",
            json={"model": CHAT_MODEL, "stream": True},
            headers=bearer(key),
        )

    assert resp.status_code == 200  # headers were already out before the kill
    assert resp.content == _PARTIAL  # exactly what arrived — truncated, no error
    assert b"[DONE]" not in resp.content


async def test_pre_first_byte_failure_retries_second_replica(
    make_harness: HarnessFactory, tmp_path: Path
) -> None:
    harness: Harness = await make_harness()
    raw = harness.client
    key = await _setup(raw, tmp_path)
    agent = await enroll_agent(raw, await mint_enrollment_token(raw))
    dead_port, live_port = reserve_ordered_ports()  # dead sorts first → picked first

    stub = StubServer(buffered_body=b'{"id":"served-by-live"}')
    await stub.start(port=live_port)  # dead_port is left closed → connection refused
    try:
        ready = (
            make_replica(CHAT_MODEL, port=dead_port, state=ReplicaState.READY),
            make_replica(CHAT_MODEL, port=live_port, state=ReplicaState.READY),
        )
        await heartbeat(agent, replicas=ready)
        resp = await raw.post(
            "/v1/chat/completions", json={"model": CHAT_MODEL}, headers=bearer(key)
        )
    finally:
        await stub.stop()

    assert resp.status_code == 200
    assert resp.content == b'{"id":"served-by-live"}'
    assert stub.hits == 1  # only the live replica was reached

    # The gateway log proves a pre-first-byte retry actually happened.
    lines = harness.config.gateway_log_path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[-1])
    assert entry["retried"] is True
    assert entry["status"] == "served"


async def test_session_affinity_reuses_one_replica_while_new_session_balances(
    make_harness: HarnessFactory, tmp_path: Path
) -> None:
    harness: Harness = await make_harness(affinity_ttl_s=60.0, affinity_max=10)
    raw = harness.client
    key = await _setup(raw, tmp_path)
    agent = await enroll_agent(raw, await mint_enrollment_token(raw))
    first_port, second_port = reserve_ordered_ports()
    first = StubServer(chunks=_SSE, content_type="text/event-stream", chunk_delay_s=0.02)
    second = StubServer(chunks=_SSE, content_type="text/event-stream", chunk_delay_s=0.02)
    await first.start(port=first_port)
    await second.start(port=second_port)
    try:
        await heartbeat(
            agent,
            replicas=(
                make_replica(CHAT_MODEL, port=first_port, state=ReplicaState.READY),
                make_replica(CHAT_MODEL, port=second_port, state=ReplicaState.READY),
            ),
        )
        body = {
            "model": CHAT_MODEL,
            "stream": True,
            "messages": [{"role": "user", "content": "keep this conversation together"}],
        }
        session_a = {**bearer(key), "X-Fallow-Session": "session-a"}
        session_b = {**bearer(key), "X-Fallow-Session": "session-b"}

        established = await raw.post("/v1/chat/completions", json=body, headers=session_a)
        held = asyncio.create_task(raw.post("/v1/chat/completions", json=body, headers=session_a))
        while first.hits < 2:
            await asyncio.sleep(0)
        balanced = await raw.post("/v1/chat/completions", json=body, headers=session_b)
        reused = await held
    finally:
        await first.stop()
        await second.stop()

    expected = b"".join(_SSE)
    assert established.content == reused.content == balanced.content == expected
    assert first.hits == 2
    assert second.hits == 1
    entries = [
        json.loads(line)
        for line in harness.config.gateway_log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [entry["affinity"] for entry in entries[-3:]] == ["miss", "hit", "miss"]
