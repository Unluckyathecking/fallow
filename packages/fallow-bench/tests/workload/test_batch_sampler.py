"""BatchDriver submit+poll recording and PowerSampler energy trace."""

from __future__ import annotations

import asyncio

import httpx
from workload_helpers import RecordingWriter, StepClock, make_clocks

from fallow_bench.workload.admin import BenchAdminClient
from fallow_bench.workload.batch import BatchDriver
from fallow_bench.workload.clocks import Clocks
from fallow_bench.workload.records import JobRecord, PowerSample
from fallow_bench.workload.sampler import PowerSampler
from fallow_protocol import AgentState, JobState, JobStatus, JobSubmit, WorkerKind


def _status(job_id: str, state: JobState, done: int) -> dict[str, object]:
    return JobStatus(
        job_id=job_id, state=state, total_units=3, done_units=done, dead_units=0
    ).model_dump(mode="json")


async def test_batch_submit_then_poll_to_done() -> None:
    responses = [
        _status("job-1", JobState.PENDING, 0),  # submit
        _status("job-1", JobState.RUNNING, 1),  # poll 1
        _status("job-1", JobState.DONE, 3),  # poll 2 -> terminal
    ]
    calls = {"n": 0}

    async def handler(_request: httpx.Request) -> httpx.Response:
        body = responses[min(calls["n"], len(responses) - 1)]
        calls["n"] += 1
        return httpx.Response(200, json=body)

    writer = RecordingWriter()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://c") as c:
        driver = BatchDriver(
            admin=BenchAdminClient(c, "k"),
            job=JobSubmit(kind=WorkerKind.EMBED, model_id="bge", payload_ref="ref"),
            submit_at_s=1.0,
            poll_interval_s=10.0,
            duration_s=1000.0,
            clocks=make_clocks(),
            writer=writer,
        )
        await driver.run()

    events = [r.event for r in writer.records]
    assert events == ["submit", "poll", "poll"]
    assert all(isinstance(r, JobRecord) for r in writer.records)
    assert writer.records[-1].state == str(JobState.DONE)
    assert writer.records[-1].done_units == 3


def _agent_json(agent_id: str, with_gpu: bool) -> dict[str, object]:
    caps = {
        "hostname": "h",
        "os": "linux",
        "os_version": "1",
        "cpu_model": "x",
        "cpu_cores": 8,
        "ram_mb": 16000,
        "disk_free_mb": 1000,
        "agent_version": "0.1.0",
    }
    gpus = (
        [{"index": 0, "vram_free_mb": 4000, "util_percent": 72.0, "power_w": 180.0}]
        if with_gpu
        else []
    )
    return {
        "agent_id": agent_id,
        "host": "100.64.0.1",
        "state": AgentState.IDLE.value,
        "suspect": False,
        "caps": caps,
        "mem_available_mb": 8000,
        "gpus": gpus,
    }


async def test_sampler_records_power_values() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=[_agent_json("gpu-box", True), _agent_json("cpu-box", False)]
        )

    writer = RecordingWriter()
    stop = asyncio.Event()

    async def one_shot_sleep(_seconds: float) -> None:
        stop.set()  # end the loop after the first round of samples
        await asyncio.sleep(0)

    clocks = Clocks(monotonic=lambda: 0.0, now=StepClock(), sleep=one_shot_sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://c") as c:
        sampler = PowerSampler(
            admin=BenchAdminClient(c, "k"), poll_hz=2.0, clocks=clocks, writer=writer
        )
        await sampler.run(stop)

    assert all(isinstance(r, PowerSample) for r in writer.records)
    gpu_rows = [r for r in writer.records if r.agent_id == "gpu-box"]
    cpu_rows = [r for r in writer.records if r.agent_id == "cpu-box"]
    assert gpu_rows[0].power_w == 180.0
    assert gpu_rows[0].util_percent == 72.0
    assert gpu_rows[0].gpu_index == 0
    assert cpu_rows[0].gpu_index is None
    assert cpu_rows[0].power_w is None


async def test_sampler_swallows_admin_errors() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    writer = RecordingWriter()
    stop = asyncio.Event()

    async def one_shot_sleep(_seconds: float) -> None:
        stop.set()
        await asyncio.sleep(0)

    clocks = Clocks(monotonic=lambda: 0.0, now=StepClock(), sleep=one_shot_sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://c") as c:
        sampler = PowerSampler(
            admin=BenchAdminClient(c, "k"), poll_hz=1.0, clocks=clocks, writer=writer
        )
        await sampler.run(stop)  # must not raise
    assert writer.records == []
