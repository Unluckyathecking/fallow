"""WorkloadRunner end-to-end over MockTransport: all JSONL streams produced."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from workload_helpers import make_clocks, sse_bytes

from fallow_bench.workload.config import (
    BatchConfig,
    ExperimentConfig,
    InteractiveConfig,
    SamplingConfig,
)
from fallow_bench.workload.runner import RunMetadata, WorkloadRunner
from fallow_protocol import AgentState, JobState, JobStatus, WorkerKind


def _config() -> ExperimentConfig:
    return ExperimentConfig(
        arm_label="test-arm",
        coordinator_url="http://coord.test",
        api_key_env="X",
        model_id="qwen",
        duration_s=5.0,
        seed=42,
        interactive=InteractiveConfig(rate_per_min=600.0, max_tokens=16, prompt_files=("p.txt",)),
        batch=BatchConfig(
            corpus_path="c.jsonl", submit_at_s=0.5, kind=WorkerKind.EMBED, model_id="bge"
        ),
        sampling=SamplingConfig(admin_poll_hz=2.0, admin_key_env="Y"),
    )


def _interactive_transport() -> httpx.MockTransport:
    async def handler(_request: httpx.Request) -> httpx.Response:
        async def gen():
            for chunk in sse_bytes(2):
                yield chunk

        return httpx.Response(200, content=gen())

    return httpx.MockTransport(handler)


def _agent_json() -> dict[str, object]:
    return {
        "agent_id": "agent-1",
        "host": "100.64.0.1",
        "state": AgentState.IDLE.value,
        "suspect": False,
        "caps": {
            "hostname": "h",
            "os": "linux",
            "os_version": "1",
            "cpu_model": "x",
            "cpu_cores": 8,
            "ram_mb": 16000,
            "disk_free_mb": 1000,
            "agent_version": "0.1.0",
        },
        "mem_available_mb": 8000,
        "gpus": [{"index": 0, "vram_free_mb": 4000, "util_percent": 40.0, "power_w": 150.0}],
    }


def _admin_transport() -> httpx.MockTransport:
    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/admin/agents":
            return httpx.Response(200, json=[_agent_json()])
        state = JobState.PENDING if request.method == "POST" else JobState.DONE
        done = 0 if request.method == "POST" else 3
        body = JobStatus(
            job_id="job-1", state=state, total_units=3, done_units=done, dead_units=0
        ).model_dump(mode="json")
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler)


def test_run_metadata_rejects_naive_started_at() -> None:
    with pytest.raises(ValidationError, match="aware UTC"):
        RunMetadata(
            started_at=datetime(2026, 7, 15, 12, 0),
            arm_label="capability",
            rep=1,
            seed=42,
            duration_s=5.0,
            config_digest="a" * 64,
            git_sha="deadbeef",
        )


async def test_runner_writes_all_streams(tmp_path: Path) -> None:
    base_dir = tmp_path / "exp"
    base_dir.mkdir()
    (base_dir / "p.txt").write_text("prompt one\nprompt two\n", encoding="utf-8")
    out_dir = tmp_path / "runs" / "test-arm"

    async with (
        httpx.AsyncClient(transport=_interactive_transport(), base_url="http://coord.test") as ic,
        httpx.AsyncClient(transport=_admin_transport(), base_url="http://coord.test") as ac,
    ):
        runner = WorkloadRunner(
            config=_config(),
            base_dir=base_dir,
            out_dir=out_dir,
            interactive_client=ic,
            admin_client=ac,
            api_key="client-key",
            admin_key="admin-key",
            clocks=make_clocks(),
            run_metadata=RunMetadata(
                started_at=datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
                arm_label="capability",
                rep=2,
                seed=42,
                duration_s=5.0,
                config_digest="a" * 64,
                git_sha="deadbeef",
            ),
        )
        result = await runner.run()

    assert result == out_dir
    schedule_lines = (out_dir / "schedule.jsonl").read_text().splitlines()
    request_lines = (out_dir / "client_trace.jsonl").read_text().splitlines()
    job_lines = (out_dir / "jobs.jsonl").read_text().splitlines()
    power_lines = (out_dir / "power.jsonl").read_text().splitlines()

    assert len(schedule_lines) >= 1
    assert len(request_lines) == len(schedule_lines)  # one record per arrival
    assert json.loads(request_lines[0])["tokens_out"] == 2
    assert [json.loads(row)["event"] for row in job_lines] == ["submit", "poll"]
    assert len(power_lines) >= 1
    assert json.loads(power_lines[0])["power_w"] == 150.0

    meta = json.loads((out_dir / "run_meta.json").read_text())
    assert meta == {
        "arm_label": "capability",
        "config_digest": "a" * 64,
        "duration_s": 5.0,
        "git_sha": "deadbeef",
        "rep": 2,
        "seed": 42,
        "started_at": "2026-07-15T12:00:00Z",
    }
