"""Scenario 2 — batch pipeline: register+assign an embed model, submit a
100-chunk corpus (→ 4 units), lease/fetch-input/complete each with the fake
embed worker until the job is DONE, then prove a resubmit dedups to instant DONE.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from conftest import Harness, HarnessFactory
from integration_helpers import (
    EMBED_MODEL,
    assign_model,
    enroll_agent,
    fake_embed_result,
    fetch_input,
    heartbeat,
    job_status,
    make_manifest,
    make_replica,
    mint_enrollment_token,
    register_model,
    submit_job,
)

from fallow_protocol.capabilities import WorkerKind
from fallow_protocol.messages import JobState, JobSubmit
from fallow_protocol.models import ReplicaState

_CHUNKS = 100
_CHUNKS_PER_UNIT = 32


def _write_corpus(tmp: Path, n: int) -> Path:
    path = tmp / "corpus.jsonl"
    path.write_text(
        "\n".join(f'{{"id": "{i}", "text": "chunk {i}"}}' for i in range(n)), encoding="utf-8"
    )
    return path


async def test_batch_pipeline_and_dedup(make_harness: HarnessFactory, tmp_path: Path) -> None:
    harness: Harness = await make_harness(chunks_per_unit=_CHUNKS_PER_UNIT)
    raw = harness.client
    expected_units = math.ceil(_CHUNKS / _CHUNKS_PER_UNIT)

    # Register the embed model (blob must exist) and enroll an idle agent.
    blob = tmp_path / "bge.gguf"
    blob.write_bytes(b"fake-gguf")
    await register_model(raw, make_manifest(EMBED_MODEL, WorkerKind.EMBED), str(blob))
    token = await mint_enrollment_token(raw)
    agent = await enroll_agent(raw, token)
    assert agent.agent_id is not None
    await assign_model(raw, EMBED_MODEL, [agent.agent_id])
    replica = make_replica(EMBED_MODEL, port=8100, state=ReplicaState.READY)
    hb = await heartbeat(agent, replicas=(replica,))
    assert hb.desired_models == (EMBED_MODEL,)  # assignment drove desired set

    # Submit the corpus → content-addressed chunking into `expected_units`.
    corpus = _write_corpus(tmp_path, _CHUNKS)
    job = JobSubmit(kind=WorkerKind.EMBED, model_id=EMBED_MODEL, payload_ref=str(corpus))
    status = await submit_job(raw, job)
    assert status.total_units == expected_units
    assert status.state == JobState.PENDING

    # Lease → fetch input → complete each unit with the fake embed worker.
    leased = 0
    while (lease := await agent.poll_work(0.0)) is not None:
        leased += 1
        assert lease.model_id == EMBED_MODEL
        assert lease.kind == WorkerKind.EMBED
        payload = await fetch_input(raw, lease.input_url, agent.device_token or "")
        assert isinstance(json.loads(payload), list)  # a batch of chunks
        await agent.complete_unit(fake_embed_result(lease))
    assert leased == expected_units

    final = await job_status(raw, status.job_id)
    assert final.state == JobState.DONE
    assert final.done_units == expected_units

    # Resubmitting the identical corpus dedups to DONE immediately (same ids).
    resubmit = await submit_job(raw, job)
    assert resubmit.state == JobState.DONE
    assert resubmit.done_units == resubmit.total_units == expected_units
