"""E1.3 smoke acceptance without external model or GPU dependencies."""

from __future__ import annotations

import asyncio
import json
import shutil
from datetime import UTC, timedelta
from pathlib import Path

import pytest
from integration_helpers import (
    CHAT_MODEL,
    EMBED_MODEL,
    Harness,
    HarnessFactory,
    bearer,
    create_api_key,
    enroll_agent,
    fake_embed_result,
    heartbeat,
    job_status,
    make_manifest,
    make_replica,
    mint_enrollment_token,
    register_model,
    submit_job,
)
from stub_server import StubServer

from fallow_agent.heartbeat import CoordinatorClient
from fallow_bench.analysis import AnalysisConfig, EnergyBaseline, build_table, load_run
from fallow_bench.analysis.recovery import failure_recovery_s
from fallow_bench.experiment.layout import create_run_layout
from fallow_bench.experiment.models import ArmName, ArmSpec, RunMode, RunSpec
from fallow_protocol.capabilities import WorkerKind
from fallow_protocol.messages import AgentEvent, EventKind, JobState, JobSubmit, WorkUnitLease
from fallow_protocol.models import ReplicaState

_OFFLINE_AFTER_S = 0.3
_ADVANCE_S = 1.0
_LEASE_DEADLINE_S = 5.0
_SSE = (
    b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n',
    b"data: [DONE]\n\n",
)


def _jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )


async def _lease_when_available(agent: CoordinatorClient) -> WorkUnitLease:
    async def _spin() -> WorkUnitLease:
        while True:
            lease = await agent.poll_work(0.0)
            if lease is not None:
                return lease
            await asyncio.sleep(0.02)

    return await asyncio.wait_for(_spin(), timeout=_LEASE_DEADLINE_S)


async def test_smoke_run_is_canonical_and_analysis_ready(
    make_harness: HarnessFactory, tmp_path: Path
) -> None:
    harness: Harness = await make_harness(
        result_dir=tmp_path / "harness-results",
        suspect_after_s=0.2,
        offline_after_s=_OFFLINE_AFTER_S,
        requeue_interval_s=0.05,
    )
    raw = harness.client

    chat_blob = tmp_path / "chat.gguf"
    chat_blob.write_bytes(b"fake-gguf")
    await register_model(raw, make_manifest(CHAT_MODEL, WorkerKind.CHAT), str(chat_blob))
    api_key = await create_api_key(raw, "e13-smoke")

    token_a = await mint_enrollment_token(raw)
    token_b = await mint_enrollment_token(raw)
    agent_a = await enroll_agent(raw, token_a, hostname="smoke-a")
    agent_b = await enroll_agent(raw, token_b, hostname="smoke-b")

    async with StubServer(chunks=_SSE, content_type="text/event-stream") as stub:
        replicas = (
            make_replica(CHAT_MODEL, port=stub.port, state=ReplicaState.READY),
            make_replica(EMBED_MODEL, port=stub.port, state=ReplicaState.READY),
        )
        await heartbeat(agent_a, replicas=replicas)
        await heartbeat(agent_b, replicas=replicas)
        response = await raw.post(
            "/v1/chat/completions",
            json={"model": CHAT_MODEL, "stream": True, "messages": []},
            headers=bearer(api_key),
        )

    assert response.status_code == 200
    assert response.content == b"".join(_SSE)
    assert stub.hits == 1

    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text('{"id":"0","text":"smoke"}\n', encoding="utf-8")
    submitted = await submit_job(
        raw,
        JobSubmit(kind=WorkerKind.EMBED, model_id=EMBED_MODEL, payload_ref=str(corpus)),
    )
    lease_a = await agent_a.poll_work(0.0)
    assert lease_a is not None
    kill_offset_s = _OFFLINE_AFTER_S

    harness.clock.advance(_ADVANCE_S)
    await heartbeat(agent_b, replicas=replicas, seq=2)
    lease_b = await _lease_when_available(agent_b)
    assert lease_b.work_unit_id == lease_a.work_unit_id
    assert lease_b.attempt == 2
    await agent_b.complete_unit(fake_embed_result(lease_b))
    assert (await job_status(raw, submitted.job_id)).state == JobState.DONE

    assert agent_a.agent_id is not None
    await agent_a.push_event(
        AgentEvent(
            agent_id=agent_a.agent_id,
            kind=EventKind.USER_RETURNED,
            at=harness.clock().astimezone(UTC),
            detail={"yield_ms": "25.0"},
        )
    )

    run = RunSpec(
        arm=ArmSpec(name=ArmName.CHURN_V2, scheduler="churn_v2", churn_enabled=True),
        repetition=1,
        seed=17,
        duration_s=120,
        mode=RunMode.SMOKE,
    )
    layout = create_run_layout(tmp_path / "runs", run)
    layout.coordinator_config.write_text("# in-process smoke coordinator\n", encoding="utf-8")
    layout.run_meta.write_text(
        json.dumps(
            {
                "started_at": (harness.clock() - timedelta(seconds=_ADVANCE_S))
                .replace(microsecond=0)
                .isoformat(),
                "arm_label": run.arm.name,
                "rep": run.repetition,
                "seed": run.seed,
                "duration_s": run.duration_s,
                "config_digest": "0" * 64,
                "git_sha": "smoke",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    _jsonl(
        layout.client_trace,
        [
            {
                "req_id": "smoke-1",
                "t_submit": 0.0,
                "t_first_token": 0.1,
                "t_done": 0.2,
                "status": "ok",
                "tokens_out": 2,
            }
        ],
    )
    shutil.copyfile(harness.config.gateway_log_path, layout.gateway)
    shutil.copyfile(harness.config.events_jsonl_path, layout.events)
    units_source = harness.config.events_jsonl_path.with_name("units.jsonl")
    shutil.copyfile(units_source, layout.units)

    # Legacy B2 logs recorded execution as an offset. run_meta supplies the
    # epoch origin, so recovery remains aligned with absolute unit timestamps.
    _jsonl(
        layout.churn,
        [
            {
                "t_executed": kill_offset_s,
                "agent": agent_a.agent_id,
                "kind": "agent_kill",
                "ok": True,
                "flip_ms": 30.0,
            }
        ],
    )
    # The ASGI smoke fleet has no GPU. An explicit empty file is the canonical
    # power baseline and distinguishes unavailable telemetry from a missing log.
    layout.power.write_text("", encoding="utf-8")
    layout.schedule.write_text("", encoding="utf-8")
    layout.jobs.write_text("", encoding="utf-8")

    assert all(path.exists() for path in layout.artifacts)
    config = AnalysisConfig(energy_baseline=EnergyBaseline(start_s=0.0, end_s=1.0))
    frames = load_run(layout.directory, config)
    assert frames.warnings == ()
    assert failure_recovery_s(frames.churn, frames.jobs) == pytest.approx(
        _ADVANCE_S - _OFFLINE_AFTER_S
    )

    table = build_table({str(run.arm.name): frames}, config)
    values = {row.label: row.values[0] for row in table.rows}
    assert values["Marginal energy per 1k tokens (J)"] is None
    assert all(
        value is not None
        for label, value in values.items()
        if label != "Marginal energy per 1k tokens (J)"
    )
