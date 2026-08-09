"""Vertical coverage for the Site Mode relay transport seam.

Drives the gateway's :class:`SiteAwareTransport` against a real
:class:`RelayBroker` and a role-playing agent coroutine, with no network. A
"direct-dial bomb" httpx client proves a site agent is never dialed on its
registered host, and the direct path is exercised byte-for-byte for parity.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence

import httpx

from fallow_coordinator.gateway.config import GatewayConfig
from fallow_coordinator.gateway.inflight import InflightTracker
from fallow_coordinator.gateway.protocols import SiteRoute
from fallow_coordinator.gateway.proxy import (
    Acquired,
    NoUpstream,
    ProxyRequest,
    SiteAwareTransport,
    SiteRelayTransport,
    UpstreamProxy,
    relay_stream_body,
)
from fallow_coordinator.site_relay import RelayBroker
from fallow_protocol.messages import ReplicaEndpoint

CHAT_PATH = "/v1/chat/completions"


def _endpoint(agent_id: str, port: int = 8100) -> ReplicaEndpoint:
    return ReplicaEndpoint(agent_id=agent_id, host="10.0.0.9", port=port, model_id="qwen2.5-7b")


def _request(body: bytes = b'{"model":"qwen2.5-7b"}') -> ProxyRequest:
    return ProxyRequest(method="POST", path=CHAT_PATH, body=body, content_type="application/json")


def _repick(endpoints: Sequence[ReplicaEndpoint]):
    def repick(tried: set[tuple[str, int]]) -> ReplicaEndpoint | None:
        for endpoint in endpoints:
            if (endpoint.host, endpoint.port) not in tried:
                return endpoint
        return None

    return repick


def _bomb_direct() -> UpstreamProxy:
    """A direct proxy whose client raises if the transport ever dials it."""

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("site agent must never be dialed on its registered host")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return UpstreamProxy(client, GatewayConfig(admission_timeout_s=0), InflightTracker())


def _site_only_resolver(*site_agents: str):
    async def resolve(agent_id: str) -> SiteRoute | None:
        return SiteRoute(presence_generation=0) if agent_id in site_agents else None

    return resolve


def _relay_transport(broker: RelayBroker, tracker: InflightTracker) -> SiteRelayTransport:
    return SiteRelayTransport(
        broker, deadline_s=5.0, monotonic=time.monotonic, first_byte_timeout_s=5.0
    )


async def _wait_for_waiter(broker: RelayBroker, agent_id: str) -> None:
    """Yield until the agent's claim is registered as a broker waiter."""
    for _ in range(1000):
        if broker._waiters.get(agent_id):
            return
        await asyncio.sleep(0)
    raise AssertionError("claim waiter never registered")


async def _collect_stream(result: Acquired) -> bytes:
    assert result.relay_stream is not None and result.hold is not None
    return b"".join(
        [
            chunk
            async for chunk in relay_stream_body(
                result.relay_stream, 5.0, result.hold, lambda: None
            )
        ]
    )


async def test_relay_stream_parity_preserves_status_media_and_raw_bytes() -> None:
    broker = RelayBroker()
    tracker = InflightTracker()
    transport = SiteAwareTransport(
        _bomb_direct(), _relay_transport(broker, tracker), _site_only_resolver("agent-s"), tracker
    )
    endpoint = _endpoint("agent-s")

    async def agent() -> None:
        claim = await broker.claim("agent-s", 0, timeout=5.0)
        assert claim is not None
        gen = claim.presence_generation
        await broker.start_response("agent-s", claim.claim_id, gen, 200, "text/event-stream")
        await broker.write("agent-s", claim.claim_id, gen, b"data: one\n\n")
        await broker.write("agent-s", claim.claim_id, gen, b"data: [DONE]\n\n")
        await broker.finish("agent-s", claim.claim_id, gen)

    task = asyncio.create_task(agent())
    await _wait_for_waiter(broker, "agent-s")
    result = await transport.acquire_stream(_request(), endpoint, _repick([endpoint]))
    assert isinstance(result, Acquired)
    assert result.relay_stream is not None
    assert result.relay_stream.status_code == 200
    assert result.relay_stream.media_type == "text/event-stream"
    assert await _collect_stream(result) == b"data: one\n\ndata: [DONE]\n\n"
    await task


async def test_relay_buffered_parity_preserves_status_and_media() -> None:
    broker = RelayBroker()
    tracker = InflightTracker()
    transport = SiteAwareTransport(
        _bomb_direct(), _relay_transport(broker, tracker), _site_only_resolver("agent-s"), tracker
    )
    endpoint = _endpoint("agent-s")

    async def agent() -> None:
        claim = await broker.claim("agent-s", 0, timeout=5.0)
        assert claim is not None
        gen = claim.presence_generation
        await broker.start_response("agent-s", claim.claim_id, gen, 201, "application/json")
        await broker.write("agent-s", claim.claim_id, gen, b'{"ok":true}')
        await broker.finish("agent-s", claim.claim_id, gen)

    task = asyncio.create_task(agent())
    await _wait_for_waiter(broker, "agent-s")
    result = await transport.acquire_buffered(_request(), endpoint, _repick([endpoint]))
    assert isinstance(result, Acquired)
    assert result.buffered is not None
    assert result.buffered.status_code == 201
    assert result.buffered.media_type == "application/json"
    assert result.buffered.body == b'{"ok":true}'
    await task


async def test_disconnected_site_agent_never_dials_its_host() -> None:
    broker = RelayBroker()
    tracker = InflightTracker()
    transport = SiteAwareTransport(
        _bomb_direct(), _relay_transport(broker, tracker), _site_only_resolver("agent-s"), tracker
    )
    endpoint = _endpoint("agent-s")
    # No claim waiter: the offer finds no claimant, so there is nothing to serve
    # and the direct-dial bomb proves we do not fall back to the registered host.
    result = await transport.acquire_buffered(_request(), endpoint, _repick([endpoint]))
    assert isinstance(result, NoUpstream)


async def test_repick_before_first_byte_then_serves_second_agent() -> None:
    broker = RelayBroker()
    tracker = InflightTracker()
    transport = SiteAwareTransport(
        _bomb_direct(),
        _relay_transport(broker, tracker),
        _site_only_resolver("agent-a", "agent-b"),
        tracker,
    )
    first, second = _endpoint("agent-a", 8100), _endpoint("agent-b", 8200)

    async def failing_agent() -> None:
        claim = await broker.claim("agent-a", 0, timeout=5.0)
        assert claim is not None
        await broker.fail("agent-a", claim.claim_id, claim.presence_generation, "connect_failed")

    async def serving_agent() -> None:
        claim = await broker.claim("agent-b", 0, timeout=5.0)
        assert claim is not None
        gen = claim.presence_generation
        await broker.start_response("agent-b", claim.claim_id, gen, 200, "application/json")
        await broker.write("agent-b", claim.claim_id, gen, b"served")
        await broker.finish("agent-b", claim.claim_id, gen)

    tasks = [asyncio.create_task(failing_agent()), asyncio.create_task(serving_agent())]
    await _wait_for_waiter(broker, "agent-a")
    await _wait_for_waiter(broker, "agent-b")
    result = await transport.acquire_buffered(_request(), first, _repick([first, second]))
    assert isinstance(result, Acquired)
    assert result.retried is True
    assert result.endpoint.agent_id == "agent-b"
    assert result.buffered is not None and result.buffered.body == b"served"
    await asyncio.gather(*tasks)


async def test_no_replay_after_first_byte_truncates() -> None:
    broker = RelayBroker()
    tracker = InflightTracker()

    async def resolve(agent_id: str) -> SiteRoute | None:
        return SiteRoute(presence_generation=0)

    transport = SiteAwareTransport(
        _bomb_direct(), _relay_transport(broker, tracker), resolve, tracker
    )
    first = _endpoint("agent-a", 8100)

    async def agent() -> None:
        claim = await broker.claim("agent-a", 0, timeout=5.0)
        assert claim is not None
        gen = claim.presence_generation
        await broker.start_response("agent-a", claim.claim_id, gen, 200, "text/event-stream")
        await broker.write("agent-a", claim.claim_id, gen, b"data: partial\n\n")
        await broker.fail("agent-a", claim.claim_id, gen, "upstream_error")

    task = asyncio.create_task(agent())
    await _wait_for_waiter(broker, "agent-a")

    def repick(tried: set[tuple[str, int]]) -> ReplicaEndpoint | None:
        raise AssertionError("must not repick after the first byte is out")

    result = await transport.acquire_stream(_request(), first, repick)
    assert isinstance(result, Acquired)
    assert result.endpoint.agent_id == "agent-a"
    # The first byte already crossed the retry boundary: the client gets the
    # buffered prefix and then a clean truncation, never a replay.
    assert await _collect_stream(result) == b"data: partial\n\n"
    await task


async def test_active_user_invalidation_before_first_byte_repicks() -> None:
    broker = RelayBroker()
    tracker = InflightTracker()
    transport = SiteAwareTransport(
        _bomb_direct(), _relay_transport(broker, tracker), _site_only_resolver("agent-s"), tracker
    )
    endpoint = _endpoint("agent-s")

    async def agent() -> None:
        claim = await broker.claim("agent-s", 0, timeout=5.0)
        assert claim is not None
        # The user returns before any byte: a newer generation invalidates the claim.
        await broker.invalidate_agent("agent-s", 1, "became_active")

    task = asyncio.create_task(agent())
    await _wait_for_waiter(broker, "agent-s")
    result = await transport.acquire_buffered(_request(), endpoint, _repick([endpoint]))
    assert isinstance(result, NoUpstream)
    await task


async def test_deadline_expiry_when_agent_never_responds() -> None:
    broker = RelayBroker()
    tracker = InflightTracker()
    relay = SiteRelayTransport(
        broker, deadline_s=5.0, monotonic=time.monotonic, first_byte_timeout_s=0.05
    )
    transport = SiteAwareTransport(_bomb_direct(), relay, _site_only_resolver("agent-s"), tracker)
    endpoint = _endpoint("agent-s")

    async def silent_agent() -> None:
        claim = await broker.claim("agent-s", 0, timeout=5.0)
        assert claim is not None  # claims, then never starts a response

    task = asyncio.create_task(silent_agent())
    await _wait_for_waiter(broker, "agent-s")
    result = await transport.acquire_buffered(_request(), endpoint, _repick([endpoint]))
    assert isinstance(result, NoUpstream)
    await task


async def test_legacy_direct_parity_when_resolver_says_direct() -> None:
    tracker = InflightTracker()
    broker = RelayBroker()

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://10.0.0.9:8100/v1/chat/completions"
        return httpx.Response(
            200, content=b"direct-body", headers={"content-type": "application/json"}
        )

    direct = UpstreamProxy(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        GatewayConfig(admission_timeout_s=0),
        tracker,
    )
    transport = SiteAwareTransport(
        direct, _relay_transport(broker, tracker), _site_only_resolver(), tracker
    )
    endpoint = _endpoint("agent-direct")
    result = await transport.acquire_buffered(_request(), endpoint, _repick([endpoint]))
    assert isinstance(result, Acquired)
    assert result.buffered is not None
    assert result.buffered.body == b"direct-body"
    assert result.buffered.status_code == 200


async def test_relayed_5xx_repicks_like_direct() -> None:
    broker = RelayBroker()
    tracker = InflightTracker()
    transport = SiteAwareTransport(
        _bomb_direct(),
        _relay_transport(broker, tracker),
        _site_only_resolver("agent-a", "agent-b"),
        tracker,
    )
    first, second = _endpoint("agent-a", 8100), _endpoint("agent-b", 8200)

    async def erroring_agent() -> None:
        claim = await broker.claim("agent-a", 0, timeout=5.0)
        assert claim is not None
        gen = claim.presence_generation
        await broker.start_response("agent-a", claim.claim_id, gen, 503, "application/json")
        await broker.write("agent-a", claim.claim_id, gen, b"upstream busy")
        await broker.finish("agent-a", claim.claim_id, gen)

    async def serving_agent() -> None:
        claim = await broker.claim("agent-b", 0, timeout=5.0)
        assert claim is not None
        gen = claim.presence_generation
        await broker.start_response("agent-b", claim.claim_id, gen, 200, "application/json")
        await broker.write("agent-b", claim.claim_id, gen, b"served")
        await broker.finish("agent-b", claim.claim_id, gen)

    tasks = [asyncio.create_task(erroring_agent()), asyncio.create_task(serving_agent())]
    await _wait_for_waiter(broker, "agent-a")
    await _wait_for_waiter(broker, "agent-b")
    result = await transport.acquire_buffered(_request(), first, _repick([first, second]))
    assert isinstance(result, Acquired)
    assert result.retried is True
    assert result.endpoint.agent_id == "agent-b"
    assert result.buffered is not None and result.buffered.body == b"served"
    await asyncio.gather(*tasks)


async def test_relayed_5xx_forwarded_when_repick_exhausted() -> None:
    broker = RelayBroker()
    tracker = InflightTracker()
    transport = SiteAwareTransport(
        _bomb_direct(), _relay_transport(broker, tracker), _site_only_resolver("agent-a"), tracker
    )
    endpoint = _endpoint("agent-a")

    async def erroring_agent() -> None:
        claim = await broker.claim("agent-a", 0, timeout=5.0)
        assert claim is not None
        gen = claim.presence_generation
        await broker.start_response("agent-a", claim.claim_id, gen, 502, "application/json")
        await broker.write("agent-a", claim.claim_id, gen, b"bad gateway")
        await broker.finish("agent-a", claim.claim_id, gen)

    task = asyncio.create_task(erroring_agent())
    await _wait_for_waiter(broker, "agent-a")
    result = await transport.acquire_buffered(_request(), endpoint, _repick([endpoint]))
    assert isinstance(result, Acquired)
    assert result.buffered is not None and result.buffered.status_code == 502
    await task


async def test_nonretryable_failure_does_not_repick() -> None:
    broker = RelayBroker()
    tracker = InflightTracker()
    nonretryable: set[str] = set()
    relay = SiteRelayTransport(
        broker,
        deadline_s=5.0,
        monotonic=time.monotonic,
        first_byte_timeout_s=5.0,
        take_nonretryable=lambda cid: cid in nonretryable,
    )
    transport = SiteAwareTransport(
        _bomb_direct(), relay, _site_only_resolver("agent-a", "agent-b"), tracker
    )
    first = _endpoint("agent-a", 8100)

    async def failing_agent() -> None:
        claim = await broker.claim("agent-a", 0, timeout=5.0)
        assert claim is not None
        nonretryable.add(claim.claim_id)  # the agent reported retryable: false
        await broker.fail("agent-a", claim.claim_id, claim.presence_generation, "upstream_error")

    def repick(tried: set[tuple[str, int]]) -> ReplicaEndpoint | None:
        raise AssertionError("a non-retryable failure must not repick")

    task = asyncio.create_task(failing_agent())
    await _wait_for_waiter(broker, "agent-a")
    result = await transport.acquire_buffered(_request(), first, repick)
    assert isinstance(result, Acquired)
    assert result.buffered is not None and result.buffered.status_code == 500
    await task


async def test_charset_qualified_json_content_type_relays() -> None:
    broker = RelayBroker()
    tracker = InflightTracker()
    transport = SiteAwareTransport(
        _bomb_direct(), _relay_transport(broker, tracker), _site_only_resolver("agent-s"), tracker
    )
    endpoint = _endpoint("agent-s")
    request = ProxyRequest(
        method="POST",
        path=CHAT_PATH,
        body=b'{"model":"qwen2.5-7b"}',
        content_type="application/json; charset=utf-8",
    )

    async def agent() -> None:
        claim = await broker.claim("agent-s", 0, timeout=5.0)
        assert claim is not None
        assert claim.request.content_type == "application/json"
        gen = claim.presence_generation
        await broker.start_response("agent-s", claim.claim_id, gen, 200, "application/json")
        await broker.write("agent-s", claim.claim_id, gen, b"ok")
        await broker.finish("agent-s", claim.claim_id, gen)

    task = asyncio.create_task(agent())
    await _wait_for_waiter(broker, "agent-s")
    result = await transport.acquire_buffered(request, endpoint, _repick([endpoint]))
    assert isinstance(result, Acquired)
    assert result.buffered is not None and result.buffered.body == b"ok"
    await task
