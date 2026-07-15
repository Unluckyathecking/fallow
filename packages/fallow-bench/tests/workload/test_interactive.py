"""InteractiveDriver: field recording, chunk counting, open-loop property."""

from __future__ import annotations

import asyncio

import httpx
from workload_helpers import (
    RecordingWriter,
    async_client,
    make_clocks,
    sse_bytes,
    sse_transport,
)

from fallow_bench.workload.interactive import InteractiveDriver
from fallow_bench.workload.records import RequestRecord, RequestStatus
from fallow_bench.workload.schedule import Arrival

_PROMPTS = ("hello", "world")


def _arrival(idx: int, offset: float, prompt_idx: int = 0) -> Arrival:
    return Arrival(idx=idx, t_offset_s=offset, prompt_idx=prompt_idx, max_tokens=32)


def _driver(client: httpx.AsyncClient, schedule, writer, timeout_s: float = 30.0):
    return InteractiveDriver(
        client=client,
        api_key="client-key",
        model_id="qwen",
        prompts=_PROMPTS,
        schedule=schedule,
        timeout_s=timeout_s,
        clocks=make_clocks(),
        writer=writer,
    )


async def test_records_fields_and_counts_chunks() -> None:
    writer = RecordingWriter()
    client = async_client(sse_transport(sse_bytes(3)))
    async with client:
        await _driver(client, [_arrival(0, 0.0, prompt_idx=1)], writer).run()
    assert len(writer.records) == 1
    rec = writer.records[0]
    assert isinstance(rec, RequestRecord)
    assert rec.req_id == 0
    assert rec.prompt_idx == 1
    assert rec.status is RequestStatus.OK
    assert rec.http_status == 200
    assert rec.tokens_out == 3  # [DONE] excluded
    assert rec.t_first_token is not None
    assert rec.t_scheduled <= rec.t_submit <= rec.t_first_token <= rec.t_done


async def test_auth_header_is_sent() -> None:
    seen: dict[str, str | None] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")

        async def gen():
            for chunk in sse_bytes(1):
                yield chunk

        return httpx.Response(200, content=gen())

    client = async_client(httpx.MockTransport(handler))
    async with client:
        await _driver(client, [_arrival(0, 0.0)], RecordingWriter()).run()
    assert seen["auth"] == "Bearer client-key"


async def test_http_error_status_recorded() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "no replica"})

    writer = RecordingWriter()
    client = async_client(httpx.MockTransport(handler))
    async with client:
        await _driver(client, [_arrival(0, 0.0)], writer).run()
    rec = writer.records[0]
    assert rec.status is RequestStatus.ERROR
    assert rec.http_status == 503
    assert rec.tokens_out == 0


async def test_connect_error_status_recorded() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    writer = RecordingWriter()
    client = async_client(httpx.MockTransport(handler))
    async with client:
        await _driver(client, [_arrival(0, 0.0)], writer).run()
    rec = writer.records[0]
    assert rec.status is RequestStatus.ERROR
    assert rec.http_status is None


async def test_per_request_timeout_recorded() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        async def gen():
            await asyncio.sleep(1.0)  # exceeds the tiny timeout below
            yield b"data: [DONE]\n\n"

        return httpx.Response(200, content=gen())

    writer = RecordingWriter()
    client = async_client(httpx.MockTransport(handler))
    async with client:
        await _driver(client, [_arrival(0, 0.0)], writer, timeout_s=0.05).run()
    rec = writer.records[0]
    assert rec.status is RequestStatus.TIMEOUT
    assert rec.tokens_out == 0


async def test_open_loop_slow_response_does_not_delay_next_arrival() -> None:
    """A slow request 0 must not block request 1 from firing/completing."""
    release = asyncio.Event()
    calls = {"n": 0}

    async def handler(_request: httpx.Request) -> httpx.Response:
        idx = calls["n"]
        calls["n"] += 1

        async def slow():
            await release.wait()
            for chunk in sse_bytes(1):
                yield chunk

        async def fast():
            for chunk in sse_bytes(2):
                yield chunk

        return httpx.Response(200, content=slow() if idx == 0 else fast())

    writer = RecordingWriter()
    client = async_client(httpx.MockTransport(handler))
    schedule = [_arrival(0, 0.0), _arrival(1, 0.1)]
    async with client:
        run_task = asyncio.create_task(_driver(client, schedule, writer).run())
        # Request 1 completes while request 0 is still blocked.
        for _ in range(1000):
            if any(getattr(r, "req_id", None) == 1 for r in writer.records):
                break
            await asyncio.sleep(0)
        done_ids = {r.req_id for r in writer.records}
        assert done_ids == {1}, "req 1 should finish before slow req 0"
        release.set()
        await run_task

    by_id = {r.req_id: r for r in writer.records}
    assert set(by_id) == {0, 1}
    # The open-loop signature: req 1 was submitted before slow req 0 finished.
    assert by_id[1].t_submit < by_id[0].t_done
