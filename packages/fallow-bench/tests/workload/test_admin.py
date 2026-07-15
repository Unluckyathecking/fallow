"""BenchAdminClient against httpx.MockTransport (no real network)."""

from __future__ import annotations

import json

import httpx
import pytest

from fallow_bench.workload.admin import BenchAdminClient
from fallow_protocol import (
    AgentState,
    JobState,
    JobStatus,
    JobSubmit,
    WorkerKind,
)


def _agent_json(agent_id: str) -> dict[str, object]:
    return {
        "agent_id": agent_id,
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
        "gpus": [{"index": 0, "vram_free_mb": 4000, "util_percent": 55.0, "power_w": 210.0}],
    }


def _job_status(job_id: str, state: JobState) -> JobStatus:
    return JobStatus(job_id=job_id, state=state, total_units=4, done_units=1, dead_units=0)


async def test_submit_job_posts_and_parses() -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json=_job_status("job-1", JobState.PENDING).model_dump(mode="json")
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://c") as c:
        admin = BenchAdminClient(c, "admin-key")
        status = await admin.submit_job(
            JobSubmit(kind=WorkerKind.EMBED, model_id="bge", payload_ref="ref")
        )
    assert status.job_id == "job-1"
    assert seen["path"] == "/v1/admin/jobs"
    assert seen["auth"] == "Bearer admin-key"
    assert seen["body"]["model_id"] == "bge"  # type: ignore[index]


async def test_get_job_parses_status() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/admin/jobs/job-9"
        return httpx.Response(
            200, json=_job_status("job-9", JobState.RUNNING).model_dump(mode="json")
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://c") as c:
        status = await BenchAdminClient(c, "k").get_job("job-9")
    assert status.state is JobState.RUNNING


async def test_list_agents_parses_snapshots() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[_agent_json("agent-1"), _agent_json("agent-2")])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://c") as c:
        agents = await BenchAdminClient(c, "k").list_agents()
    assert [a.agent_id for a in agents] == ["agent-1", "agent-2"]
    assert agents[0].gpus[0].power_w == 210.0


async def test_http_error_propagates() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "nope"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://c") as c:
        with pytest.raises(httpx.HTTPStatusError):
            await BenchAdminClient(c, "k").list_agents()
