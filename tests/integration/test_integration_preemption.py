"""Scenario 4 — preemption reaction.

Part A: a ``user_returned`` event flips the agent's app-layer routing state so the
batch long-poll sheds it to 204 immediately; ``user_idle`` restores leasing.

Part B: because the gateway reads agent state from the registry (ADR 014), an
ACTIVE heartbeat (which the real agent sends within ≤5s of the user returning)
removes the agent's replica from interactive routing (503 SHED); an IDLE
heartbeat restores it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
from integration_helpers import (
    CHAT_MODEL,
    EMBED_MODEL,
    Harness,
    HarnessFactory,
    bearer,
    create_api_key,
    enroll_agent,
    heartbeat,
    make_manifest,
    make_replica,
    mint_enrollment_token,
    push_event,
    register_model,
    submit_job,
)
from stub_server import StubServer

from fallow_protocol.capabilities import WorkerKind
from fallow_protocol.messages import AgentState, EventKind, JobSubmit
from fallow_protocol.models import ReplicaState


async def test_user_returned_sheds_batch_longpoll(
    make_harness: HarnessFactory, tmp_path: Path
) -> None:
    harness: Harness = await make_harness()
    raw = harness.client
    agent = await enroll_agent(raw, await mint_enrollment_token(raw))
    await heartbeat(agent, replicas=(make_replica(EMBED_MODEL, port=8100),))

    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text('{"id": "0", "text": "only chunk"}', encoding="utf-8")
    # One leasable unit exists for this agent's READY replica.
    await submit_job(
        raw, JobSubmit(kind=WorkerKind.EMBED, model_id=EMBED_MODEL, payload_ref=str(corpus))
    )

    # user_returned → override ACTIVE → the pending unit is shed (no lease).
    await push_event(agent, EventKind.USER_RETURNED)
    assert await agent.poll_work(0.0) is None

    # user_idle → override IDLE → the same unit now leases (event was the gate).
    await push_event(agent, EventKind.USER_IDLE)
    restored = await agent.poll_work(0.0)
    assert restored is not None
    assert restored.model_id == EMBED_MODEL


async def _chat(raw: httpx.AsyncClient, key: str) -> int:
    resp = await raw.post("/v1/chat/completions", json={"model": CHAT_MODEL}, headers=bearer(key))
    return int(resp.status_code)


async def test_active_heartbeat_excludes_from_gateway(
    make_harness: HarnessFactory, tmp_path: Path
) -> None:
    harness: Harness = await make_harness()
    raw = harness.client
    blob = tmp_path / "chat.gguf"
    blob.write_bytes(b"fake-gguf")
    await register_model(raw, make_manifest(CHAT_MODEL, WorkerKind.CHAT), str(blob))
    key = await create_api_key(raw, "team-a")
    agent = await enroll_agent(raw, await mint_enrollment_token(raw))

    async with StubServer(buffered_body=b'{"id":"cmpl-1"}') as stub:
        ready = (make_replica(CHAT_MODEL, port=stub.port, state=ReplicaState.READY),)
        # IDLE + READY → the gateway routes to the loopback stub.
        await heartbeat(agent, state=AgentState.IDLE, replicas=ready)
        assert await _chat(raw, key) == 200

        # ACTIVE heartbeat → registry excludes the replica → shed with 503.
        await heartbeat(agent, state=AgentState.ACTIVE, replicas=ready, seq=2)
        assert await _chat(raw, key) == 503

        # IDLE again → routing restored.
        await heartbeat(agent, state=AgentState.IDLE, replicas=ready, seq=3)
        assert await _chat(raw, key) == 200


async def test_reclaim_reroutes_then_sheds_then_restores(
    make_harness: HarnessFactory, tmp_path: Path
) -> None:
    """Two agents serve one chat model; reclaiming excludes an agent from routing.

    Reclaim one → traffic still lands on the other. Reclaim both → the request
    sheds cleanly (503). Release one → serving is restored.
    """
    harness: Harness = await make_harness()
    raw = harness.client
    blob = tmp_path / "chat.gguf"
    blob.write_bytes(b"fake-gguf")
    await register_model(raw, make_manifest(CHAT_MODEL, WorkerKind.CHAT), str(blob))
    key = await create_api_key(raw, "team-a")
    agent_a = await enroll_agent(raw, await mint_enrollment_token(raw), hostname="pc-a")
    agent_b = await enroll_agent(raw, await mint_enrollment_token(raw), hostname="pc-b")

    async with StubServer(buffered_body=b'{"id":"cmpl-1"}') as stub:
        ready = (make_replica(CHAT_MODEL, port=stub.port, state=ReplicaState.READY),)

        # Both serving → routable.
        await heartbeat(agent_a, replicas=ready)
        await heartbeat(agent_b, replicas=ready)
        assert await _chat(raw, key) == 200

        # Reclaim A → B still serves the same model.
        await heartbeat(agent_a, replicas=ready, serving_paused=True, seq=2)
        assert await _chat(raw, key) == 200

        # Reclaim B too → no candidate left, the request sheds.
        await heartbeat(agent_b, replicas=ready, serving_paused=True, seq=2)
        assert await _chat(raw, key) == 503

        # Release A → serving restored.
        await heartbeat(agent_a, replicas=ready, serving_paused=False, seq=3)
        assert await _chat(raw, key) == 200


async def test_admission_queue_bridges_preempt_to_idle_window(
    make_harness: HarnessFactory, tmp_path: Path
) -> None:
    harness: Harness = await make_harness(admission_timeout_s=0.5, admission_capacity=2)
    raw = harness.client
    blob = tmp_path / "queued-chat.gguf"
    blob.write_bytes(b"fake-gguf")
    await register_model(raw, make_manifest(CHAT_MODEL, WorkerKind.CHAT), str(blob))
    key = await create_api_key(raw, "queued-team")
    agent = await enroll_agent(raw, await mint_enrollment_token(raw))

    async with StubServer(buffered_body=b'{"id":"cmpl-queued"}') as stub:
        ready = (make_replica(CHAT_MODEL, port=stub.port, state=ReplicaState.READY),)
        await heartbeat(agent, state=AgentState.ACTIVE, replicas=ready)
        pending = asyncio.create_task(
            raw.post("/v1/chat/completions", json={"model": CHAT_MODEL}, headers=bearer(key))
        )
        await asyncio.sleep(0.05)
        assert not pending.done()

        await heartbeat(agent, state=AgentState.IDLE, replicas=ready, seq=2)
        response = await pending

    assert response.status_code == 200
    entries = [
        json.loads(line)
        for line in harness.config.gateway_log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert entries[-1]["status"] == "served"
    assert entries[-1]["waited_ms"] >= 50
