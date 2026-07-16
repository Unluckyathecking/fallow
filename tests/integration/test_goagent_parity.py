"""E4.4 parity harness — run the shared integration scenarios against the Go agent.

These mirror the Python agent scenarios (lifecycle, batch, preemption, churn) but
swap the agent uplink for the built Go binary driving a coordinator over a real
loopback socket. The admin-side setup and the coordinator-state assertions are the
*same* helpers the Python tests use, so this suite is the acceptance boundary: it
exposes semantic differences between the two agents rather than hiding them behind
Go-specific checks. Byte-level gateway streaming stays Python-only (the agent is
not in that path). Preemption Part A (the ``user_returned`` push_event flipping
long-poll shedding) also stays Python-only for now: ``agentctl`` has no
``push-event`` subcommand, so that gap is deferred rather than covered here.

Two flavours of Go agent are exercised:

- Most scenarios shell the one-shot ``agentctl`` subcommands (register, heartbeat,
  poll, upload, complete) through :class:`GoAgent`, threading agent_id/token by
  hand. This gives fine-grained control the batch/eviction assertions need.
- ``test_goagent_daemon_*`` launches ``agentctl run`` as the real persistent
  daemon (:func:`run_daemon`) and asserts it enrolls, heartbeats itself visible,
  and shuts down cleanly on signal — the composed run loop, not individual calls.
  Serving and batch execution stay on the one-shot path until a Go worker lands.

The whole module is marked ``goagent`` and skips when no binary is present.
"""

from __future__ import annotations

import asyncio
import json
import math
import stat
import sys
from pathlib import Path

import pytest
from conftest import make_go_agent
from goagent import GoAgent, GoAgentError, run_daemon, write_agent_config
from integration_helpers import (
    CHAT_MODEL,
    EMBED_MODEL,
    assign_model,
    bearer,
    create_api_key,
    fetch_result_payload,
    job_status,
    list_agents,
    make_manifest,
    make_replica,
    mint_enrollment_token,
    register_model,
    submit_job,
)
from live_coordinator import LiveCoordinator
from stub_server import StubServer

from fallow_protocol.capabilities import WorkerKind
from fallow_protocol.messages import AgentState, JobState, JobSubmit
from fallow_protocol.models import ReplicaState

pytestmark = pytest.mark.goagent

LiveFactory = object  # a `make_live_coordinator` factory; typed loosely for tests.

_LEASE_DEADLINE_S = 5.0
_DAEMON_DEADLINE_S = 10.0


async def _lease_when_available(agent: GoAgent) -> dict:
    """Poll (bounded) until the eviction loop requeues a unit this agent can take."""

    async def _spin() -> dict:
        while True:
            lease = await agent.poll_work(0.0)
            if lease is not None:
                return lease
            await asyncio.sleep(0.02)

    return await asyncio.wait_for(_spin(), timeout=_LEASE_DEADLINE_S)


async def test_goagent_lifecycle_enroll_heartbeat_visible(
    make_live_coordinator: LiveFactory, go_agent_binary: Path, tmp_path: Path
) -> None:
    coordinator: LiveCoordinator = await make_live_coordinator()
    agent = make_go_agent(go_agent_binary, coordinator)

    # First-run enrollment through the Go client, persisted 0600.
    state_file = tmp_path / "agent-state.json"
    token = await mint_enrollment_token(coordinator.client)
    await agent.register(token, state_path=state_file)
    assert agent.agent_id
    assert state_file.exists()
    if sys.platform != "win32":  # Windows has no POSIX modes
        assert stat.S_IMODE(state_file.stat().st_mode) == 0o600

    # Heartbeat carrying a READY replica; no assignment, so no desired models.
    replica = make_replica(CHAT_MODEL, port=8080, state=ReplicaState.READY)
    desired = await agent.heartbeat(state=AgentState.IDLE, replicas=(replica,))
    assert desired == ()

    agents = await list_agents(coordinator.client)
    assert [a.agent_id for a in agents] == [agent.agent_id]
    snap = agents[0]
    assert snap.state == AgentState.IDLE
    assert [r.model_id for r in snap.replicas] == [CHAT_MODEL]
    assert snap.replicas[0].state == ReplicaState.READY


async def test_goagent_batch_pipeline(
    make_live_coordinator: LiveFactory, go_agent_binary: Path, tmp_path: Path
) -> None:
    chunks, per_unit = 3, 2
    expected_units = math.ceil(chunks / per_unit)
    coordinator: LiveCoordinator = await make_live_coordinator(chunks_per_unit=per_unit)
    raw = coordinator.client

    blob = tmp_path / "bge.gguf"
    blob.write_bytes(b"fake-gguf")
    await register_model(raw, make_manifest(EMBED_MODEL, WorkerKind.EMBED), str(blob))
    agent = make_go_agent(go_agent_binary, coordinator)
    await agent.register(await mint_enrollment_token(raw))
    assert agent.agent_id is not None
    await assign_model(raw, EMBED_MODEL, [agent.agent_id])

    replica = make_replica(EMBED_MODEL, port=8100, state=ReplicaState.READY)
    desired = await agent.heartbeat(replicas=(replica,))
    assert desired == (EMBED_MODEL,)  # assignment drove the desired set

    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text(
        "\n".join(f'{{"id": "{i}", "text": "chunk {i}"}}' for i in range(chunks)),
        encoding="utf-8",
    )
    job = JobSubmit(kind=WorkerKind.EMBED, model_id=EMBED_MODEL, payload_ref=str(corpus))
    status = await submit_job(raw, job)
    assert status.total_units == expected_units
    assert status.state == JobState.PENDING

    leased = 0
    expected_payloads: dict[str, bytes] = {}
    while (lease := await agent.poll_work(0.0)) is not None:
        leased += 1
        assert lease["model_id"] == EMBED_MODEL
        assert lease["kind"] == WorkerKind.EMBED.value
        payload = json.dumps(
            {"work_unit_id": lease["work_unit_id"], "vectors": [[0.25, 0.75]]},
            separators=(",", ":"),
        ).encode()
        ref = await agent.upload_result(
            lease["work_unit_id"], attempt=lease["attempt"], payload=payload
        )
        await agent.complete_unit(lease["work_unit_id"], attempt=lease["attempt"], result_ref=ref)
        expected_payloads[lease["work_unit_id"]] = payload
    assert leased == expected_units

    final = await job_status(raw, status.job_id)
    assert final.state == JobState.DONE
    assert final.done_units == expected_units
    for work_unit_id, expected_payload in expected_payloads.items():
        assert await fetch_result_payload(raw, work_unit_id) == expected_payload

    # Resubmitting the identical corpus dedups to DONE immediately (same ids).
    resubmit = await submit_job(raw, job)
    assert resubmit.state == JobState.DONE
    assert resubmit.done_units == resubmit.total_units == expected_units


async def _chat(raw: object, key: str) -> int:
    resp = await raw.post(  # type: ignore[attr-defined]
        "/v1/chat/completions",
        json={"model": CHAT_MODEL, "messages": [{"role": "system", "content": "ping"}]},
        headers=bearer(key),
    )
    return int(resp.status_code)


async def test_goagent_active_heartbeat_excludes_from_gateway(
    make_live_coordinator: LiveFactory, go_agent_binary: Path, tmp_path: Path
) -> None:
    coordinator: LiveCoordinator = await make_live_coordinator()
    raw = coordinator.client
    blob = tmp_path / "chat.gguf"
    blob.write_bytes(b"fake-gguf")
    await register_model(raw, make_manifest(CHAT_MODEL, WorkerKind.CHAT), str(blob))
    key = await create_api_key(raw, "team-a")
    agent = make_go_agent(go_agent_binary, coordinator)
    await agent.register(await mint_enrollment_token(raw))

    async with StubServer(buffered_body=b'{"id":"cmpl-1"}') as stub:
        ready = (make_replica(CHAT_MODEL, port=stub.port, state=ReplicaState.READY),)
        # IDLE + READY → the gateway routes to the loopback bench listener.
        await agent.heartbeat(state=AgentState.IDLE, replicas=ready)
        assert await _chat(raw, key) == 200

        # ACTIVE heartbeat → the registry excludes the replica → shed with 503.
        await agent.heartbeat(state=AgentState.ACTIVE, replicas=ready, seq=2)
        assert await _chat(raw, key) == 503

        # IDLE again → routing restored.
        await agent.heartbeat(state=AgentState.IDLE, replicas=ready, seq=3)
        assert await _chat(raw, key) == 200


async def test_goagent_offline_requeue(
    make_live_coordinator: LiveFactory, go_agent_binary: Path, tmp_path: Path
) -> None:
    coordinator: LiveCoordinator = await make_live_coordinator(
        suspect_after_s=0.2, offline_after_s=0.3, requeue_interval_s=0.05
    )
    raw = coordinator.client
    replica = (make_replica(EMBED_MODEL, port=8100, state=ReplicaState.READY),)

    agent_a = make_go_agent(go_agent_binary, coordinator)
    agent_b = make_go_agent(go_agent_binary, coordinator)
    await agent_a.register(await mint_enrollment_token(raw), hostname="pc-a")
    await agent_b.register(await mint_enrollment_token(raw), hostname="pc-b")
    await agent_a.heartbeat(replicas=replica)
    await agent_b.heartbeat(replicas=replica)

    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text('{"id": "0", "text": "only chunk"}', encoding="utf-8")
    job = JobSubmit(kind=WorkerKind.EMBED, model_id=EMBED_MODEL, payload_ref=str(corpus))
    status = await submit_job(raw, job)
    assert status.total_units == 1

    # Agent A leases the only unit (attempt 1), then falls silent.
    lease_a = await agent_a.poll_work(0.0)
    assert lease_a is not None
    assert lease_a["attempt"] == 1

    # Time passes A's offline threshold; agent B keeps beating (stays online).
    coordinator.clock.advance(1.0)
    await agent_b.heartbeat(replicas=replica, seq=2)

    # The eviction loop requeues A's abandoned unit; B leases it (attempt 2).
    lease_b = await _lease_when_available(agent_b)
    assert lease_b["work_unit_id"] == lease_a["work_unit_id"]
    assert lease_b["attempt"] == 2
    ref_b = await agent_b.upload_result(
        lease_b["work_unit_id"], attempt=lease_b["attempt"], payload=b"agent-b result"
    )
    await agent_b.complete_unit(
        lease_b["work_unit_id"], attempt=lease_b["attempt"], result_ref=ref_b
    )

    done = await job_status(raw, status.job_id)
    assert done.state == JobState.DONE
    assert done.done_units == 1

    # Exactly-once: the evicted agent's stale attempt is rejected (409).
    with pytest.raises(GoAgentError):
        await agent_a.complete_unit(lease_a["work_unit_id"], attempt=lease_a["attempt"])
    still_done = await job_status(raw, status.job_id)
    assert still_done.state == JobState.DONE
    assert still_done.done_units == 1


async def _wait_for_visible_agent(client: object) -> object:
    """Poll (bounded) until exactly one agent is registered and heartbeating."""

    async def _spin() -> object:
        while True:
            agents = await list_agents(client)  # type: ignore[arg-type]
            if agents:
                return agents[0]
            await asyncio.sleep(0.05)

    return await asyncio.wait_for(_spin(), timeout=_DAEMON_DEADLINE_S)


async def test_goagent_daemon_enroll_heartbeat_visible(
    make_live_coordinator: LiveFactory, go_agent_binary: Path, tmp_path: Path
) -> None:
    """The real ``agentctl run`` daemon enrolls, heartbeats visible, and stops clean.

    This is the composed run loop rather than one-shot calls: the daemon resolves
    identity, starts its own heartbeat/preempt/work loops, and tears down on a
    signal. Serving and batch execution remain on the one-shot path above.
    """
    coordinator: LiveCoordinator = await make_live_coordinator()
    token = await mint_enrollment_token(coordinator.client)
    config_path = tmp_path / "agent.toml"
    state_path = tmp_path / "agent-state.json"
    write_agent_config(
        config_path,
        coordinator_url=coordinator.base_url,
        enrollment_token=token,
        state_path=state_path,
    )

    async with run_daemon(go_agent_binary, config_path) as daemon:
        snap = await _wait_for_visible_agent(coordinator.client)
        # It is registered and heartbeating. State is whatever the host's idle
        # detector reports — IDLE on a headless/unsupported host, ACTIVE on a
        # developer machine with real input — so accept either running state and
        # only reject the terminal DRAINING here.
        assert snap.state in (AgentState.IDLE, AgentState.ACTIVE)

        # First-run enrollment persisted the identity 0600, same as the one-shot path.
        assert state_path.exists()
        if sys.platform != "win32":
            assert stat.S_IMODE(state_path.stat().st_mode) == 0o600

        code = await daemon.stop()

    # POSIX gets a graceful SIGINT shutdown; assert the clean exit. Windows kills
    # the child (no catchable console signal), so only the visibility above is checked.
    if sys.platform != "win32":
        assert code == 0, daemon.stderr
