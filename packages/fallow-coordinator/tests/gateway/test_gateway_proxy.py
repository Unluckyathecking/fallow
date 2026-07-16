"""Proxying: verbatim passthrough, streaming bytes, retry policy, inflight."""

import httpx
from gateway_helpers import (
    ADMIN_KEY,
    CHAT_MODEL,
    buffered_handler,
    make_endpoint,
    sse_handler,
)

from fallow_coordinator.gateway import LogStatus

_AUTH = {"Authorization": f"Bearer {ADMIN_KEY}"}
_ONE = {CHAT_MODEL: (make_endpoint("h1", 8001),)}
_TWO = {CHAT_MODEL: (make_endpoint("h1", 8001), make_endpoint("h2", 8002, agent_id="agent-2"))}
_MSG = [{"role": "user", "content": "hi"}]

_SSE_CHUNKS = [
    b'data: {"choices":[{"delta":{"content":"He"}}]}\n\n',
    b'data: {"choices":[{"delta":{"content":"llo"}}]}\n\n',
    b"data: [DONE]\n\n",
]


async def test_non_stream_passthrough(build_gateway) -> None:
    harness = await build_gateway(
        upstream_handler=buffered_handler(b'{"id":"cmpl-1"}'), endpoints=_ONE
    )
    response = await harness.client.post(
        "/v1/chat/completions", json={"model": CHAT_MODEL, "messages": _MSG}, headers=_AUTH
    )
    assert response.status_code == 200
    assert response.content == b'{"id":"cmpl-1"}'
    assert len(harness.log.entries) == 1
    entry = harness.log.entries[0]
    assert entry.status is LogStatus.SERVED
    assert entry.agent_id == "agent-1"
    assert entry.retried is False


async def test_streaming_bytes_pass_through_exactly(build_gateway) -> None:
    harness = await build_gateway(upstream_handler=sse_handler(_SSE_CHUNKS), endpoints=_ONE)
    response = await harness.client.post(
        "/v1/chat/completions",
        json={"model": CHAT_MODEL, "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        headers=_AUTH,
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.content == b"".join(_SSE_CHUNKS)  # byte-for-byte incl [DONE]
    entry = harness.log.entries[0]
    assert entry.status is LogStatus.SERVED
    assert entry.t_first_byte is not None
    assert entry.prompt_chars == 2  # len("hi")


async def test_connect_error_before_first_byte_retries_second_endpoint(build_gateway) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.port == 8001:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, content=b'{"from":"h2"}')

    harness = await build_gateway(upstream_handler=handler, endpoints=_TWO)
    response = await harness.client.post(
        "/v1/chat/completions", json={"model": CHAT_MODEL, "messages": _MSG}, headers=_AUTH
    )
    assert response.status_code == 200
    assert response.content == b'{"from":"h2"}'
    entry = harness.log.entries[0]
    assert entry.status is LogStatus.SERVED
    assert entry.retried is True
    assert entry.agent_id == "agent-2"


async def test_no_retry_after_first_byte_truncates(build_gateway) -> None:
    hit_ports: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        hit_ports.append(request.url.port or 0)

        async def gen():
            yield b"data: partial\n\n"
            raise httpx.ReadError("upstream died mid-stream")

        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=gen())

    harness = await build_gateway(upstream_handler=handler, endpoints=_TWO)
    response = await harness.client.post(
        "/v1/chat/completions",
        json={"model": CHAT_MODEL, "stream": True, "messages": _MSG},
        headers=_AUTH,
    )
    assert response.content == b"data: partial\n\n"  # truncated at the failure
    assert hit_ports == [8001]  # never failed over to the second endpoint
    entry = harness.log.entries[0]
    assert entry.status is LogStatus.SERVED
    assert entry.retried is False


async def test_inflight_increments_then_decrements(build_gateway) -> None:
    seen: dict[str, dict[tuple[str, int], int]] = {}
    holder: dict[str, object] = {}

    def handler(_request: httpx.Request) -> httpx.Response:
        get_inflight = holder["get_inflight"]
        seen["during"] = get_inflight()  # type: ignore[operator]
        return httpx.Response(200, content=b"{}")

    harness = await build_gateway(upstream_handler=handler, endpoints=_ONE)
    holder["get_inflight"] = harness.router.get_inflight
    response = await harness.client.post(
        "/v1/chat/completions", json={"model": CHAT_MODEL, "messages": _MSG}, headers=_AUTH
    )
    assert response.status_code == 200
    assert seen["during"] == {("h1", 8001): 1}  # held during the proxied call
    assert harness.router.get_inflight() == {}  # released after completion
