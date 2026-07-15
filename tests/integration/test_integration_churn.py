"""Scenario 3 — churn: agent A leases a unit then goes silent; the clock passes
the (tiny) offline threshold; the real eviction loop requeues; agent B leases the
same unit (attempts incremented) and completes it. A late completion from the
now-offline agent A is a silent no-op — the result is exactly-once.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from integration_helpers import (
    EMBED_MODEL,
    Harness,
    HarnessFactory,
    enroll_agent,
    fake_embed_result,
    heartbeat,
    job_status,
    make_replica,
    mint_enrollment_token,
    submit_job,
)

from fallow_agent.heartbeat import CoordinatorClient
from fallow_bench.analysis.loaders import load_churn, load_jobs
from fallow_bench.analysis.recovery import failure_recovery_s
from fallow_protocol.capabilities import WorkerKind
from fallow_protocol.messages import JobState, JobSubmit, WorkUnitLease
from fallow_protocol.models import ReplicaState

_OFFLINE_AFTER_S = 0.3
_ADVANCE_S = 1.0
_LEASE_DEADLINE_S = 5.0


async def _lease_when_available(agent: CoordinatorClient) -> WorkUnitLease:
    """Poll (bounded) until the eviction loop requeues a unit this agent can take."""

    async def _spin() -> WorkUnitLease:
        while True:
            lease = await agent.poll_work(0.0)
            if lease is not None:
                return lease
            await asyncio.sleep(0.02)

    return await asyncio.wait_for(_spin(), timeout=_LEASE_DEADLINE_S)


async def test_churn_eviction_requeues_to_second_agent(
    make_harness: HarnessFactory, tmp_path: Path
) -> None:
    harness: Harness = await make_harness(
        suspect_after_s=0.2,
        offline_after_s=_OFFLINE_AFTER_S,
        requeue_interval_s=0.05,
    )
    raw = harness.client
    replica = (make_replica(EMBED_MODEL, port=8100, state=ReplicaState.READY),)

    token_a = await mint_enrollment_token(raw)
    token_b = await mint_enrollment_token(raw)
    agent_a = await enroll_agent(raw, token_a, hostname="pc-a")
    agent_b = await enroll_agent(raw, token_b, hostname="pc-b")
    await heartbeat(agent_a, replicas=replica)
    await heartbeat(agent_b, replicas=replica)

    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text('{"id": "0", "text": "only chunk"}', encoding="utf-8")
    job = JobSubmit(kind=WorkerKind.EMBED, model_id=EMBED_MODEL, payload_ref=str(corpus))
    status = await submit_job(raw, job)
    assert status.total_units == 1

    # Agent A leases the only unit (attempt 1), then falls silent.
    lease_a = await agent_a.poll_work(0.0)
    assert lease_a is not None
    assert lease_a.attempt == 1
    offline_at_s = harness.clock().timestamp() + _OFFLINE_AFTER_S

    # Time passes A's offline threshold; agent B keeps beating (stays online).
    harness.clock.advance(_ADVANCE_S)
    await heartbeat(agent_b, replicas=replica, seq=2)

    # The eviction loop requeues A's abandoned unit; B leases it (attempt 2).
    lease_b = await _lease_when_available(agent_b)
    assert lease_b.work_unit_id == lease_a.work_unit_id
    assert lease_b.attempt == 2

    await agent_b.complete_unit(fake_embed_result(lease_b))
    done = await job_status(raw, status.job_id)
    assert done.state == JobState.DONE
    assert done.done_units == 1

    # Exactly-once: a late completion from the evicted agent A is ignored.
    await agent_a.complete_unit(fake_embed_result(lease_a))
    still_done = await job_status(raw, status.job_id)
    assert still_done.state == JobState.DONE
    assert still_done.done_units == 1

    units_path = harness.config.events_jsonl_path.with_name("units.jsonl")
    transitions = [json.loads(line) for line in units_path.read_text(encoding="utf-8").splitlines()]
    assert [
        (
            row["work_unit_id"],
            row["job_id"],
            row["agent_id"],
            row["attempt"],
            row["state"],
        )
        for row in transitions
    ] == [
        (lease_a.work_unit_id, status.job_id, agent_a.agent_id, 1, "leased"),
        (lease_a.work_unit_id, status.job_id, agent_a.agent_id, 1, "pending"),
        (lease_b.work_unit_id, status.job_id, agent_b.agent_id, 2, "leased"),
        (lease_b.work_unit_id, status.job_id, agent_b.agent_id, 2, "done"),
    ]
    expected_keys = {"work_unit_id", "job_id", "agent_id", "attempt", "state", "t"}
    assert all(set(row) == expected_keys for row in transitions)

    churn_path = units_path.with_name("churn.jsonl")
    churn_path.write_text(
        json.dumps(
            {
                "t_epoch": offline_at_s,
                "t_executed": _OFFLINE_AFTER_S,
                "agent": agent_a.agent_id,
                "kind": "agent_kill",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    churn, churn_warnings = load_churn(churn_path)
    jobs, job_warnings = load_jobs(units_path)
    assert churn_warnings == []
    assert job_warnings == []
    assert failure_recovery_s(churn, jobs) == pytest.approx(_ADVANCE_S - _OFFLINE_AFTER_S)
