"""Session affinity expiry, eviction, derivation, and gateway remapping."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import httpx
from gateway_helpers import ADMIN_KEY, CHAT_MODEL, make_endpoint

from fallow_coordinator.gateway import AffinityMap, AffinityState, GatewayConfig
from fallow_coordinator.gateway.bodyparse import parse_body
from fallow_coordinator.gateway.session import derive_session_key
from fallow_protocol.messages import ReplicaEndpoint

_AUTH = {"Authorization": f"Bearer {ADMIN_KEY}"}
_SESSION = {**_AUTH, "X-Fallow-Session": "conversation-7"}
_ONE = make_endpoint("h1", 8001)
_TWO = make_endpoint("h2", 8002, agent_id="agent-2")


class MutableClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


class AlternatingPick:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, candidates: Sequence[ReplicaEndpoint]) -> ReplicaEndpoint | None:
        if not candidates:
            return None
        selected = candidates[self.calls % len(candidates)]
        self.calls += 1
        return selected


def test_affinity_hit_refreshes_sliding_ttl() -> None:
    clock = MutableClock()
    affinity = AffinityMap(ttl_s=10, max_entries=10, now=clock)
    pick = AlternatingPick()

    first = affinity.resolve("session", (_ONE, _TWO), pick)
    clock.advance(9)
    second = affinity.resolve("session", (_ONE, _TWO), pick)
    clock.advance(9)
    third = affinity.resolve("session", (_ONE, _TWO), pick)

    assert first.endpoint == _ONE
    assert first.state is AffinityState.MISS
    assert second.endpoint == _ONE
    assert second.state is AffinityState.HIT
    assert third.endpoint == _ONE
    assert third.state is AffinityState.HIT
    assert pick.calls == 1


def test_expired_mapping_returns_to_scheduler() -> None:
    clock = MutableClock()
    affinity = AffinityMap(ttl_s=10, max_entries=10, now=clock)
    pick = AlternatingPick()

    assert affinity.resolve("session", (_ONE, _TWO), pick).endpoint == _ONE
    clock.advance(10)
    remapped = affinity.resolve("session", (_ONE, _TWO), pick)

    assert remapped.endpoint == _TWO
    assert remapped.state is AffinityState.MISS


def test_unhealthy_mapping_is_dropped_before_reuse() -> None:
    clock = MutableClock()
    affinity = AffinityMap(ttl_s=60, max_entries=10, now=clock)
    affinity.resolve("session", (_ONE, _TWO), lambda candidates: candidates[0])

    remapped = affinity.resolve("session", (_TWO,), lambda candidates: candidates[0])

    assert remapped.endpoint == _TWO
    assert remapped.state is AffinityState.MISS


def test_lru_capacity_evicts_least_recently_used_session() -> None:
    clock = MutableClock()
    affinity = AffinityMap(ttl_s=60, max_entries=2, now=clock)
    pick = AlternatingPick()
    affinity.resolve("old", (_ONE, _TWO), pick)
    affinity.resolve("kept", (_ONE, _TWO), pick)
    affinity.resolve("old", (_ONE, _TWO), pick)
    affinity.resolve("new", (_ONE, _TWO), pick)

    decision = affinity.resolve("kept", (_ONE, _TWO), pick)

    assert decision.state is AffinityState.MISS


def test_session_derivation_prefers_header_and_bounds_message_prefix() -> None:
    first = parse_body(b'{"model":"m","messages":[{"role":"user","content":"hello"}]}')
    same_prefix = parse_body(
        ('{"model":"m","messages":[{"role":"user","content":"' + "x" * 256 + 'one"}]}').encode()
    )
    other_suffix = parse_body(
        ('{"model":"m","messages":[{"role":"user","content":"' + "x" * 256 + 'two"}]}').encode()
    )
    assert first is not None and same_prefix is not None and other_suffix is not None

    explicit = derive_session_key("m", "named", "api-a", first)
    assert explicit != derive_session_key("m", "named", "api-b", first)
    assert derive_session_key("m", None, "api-a", same_prefix) == derive_session_key(
        "m", None, "api-a", other_suffix
    )
    assert derive_session_key("m", None, "api-a", first) != derive_session_key(
        "m", None, "api-b", first
    )


def test_request_without_session_or_user_message_has_no_affinity() -> None:
    parsed = parse_body(b'{"model":"m","input":"embedding text"}')
    assert parsed is not None
    assert derive_session_key("m", None, "api-a", parsed) is None


async def test_retry_rebinds_session_to_endpoint_that_served(build_gateway) -> None:
    hit_ports: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        port = request.url.port or 0
        hit_ports.append(port)
        if port == 8001:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, content=b'{"from":"h2"}')

    endpoints = {CHAT_MODEL: (_ONE, _TWO)}
    harness = await build_gateway(
        upstream_handler=handler,
        endpoints=endpoints,
        config=GatewayConfig(affinity_ttl_s=60, affinity_max=10),
    )
    body = {"model": CHAT_MODEL, "messages": [{"role": "user", "content": "hello"}]}

    first = await harness.client.post("/v1/chat/completions", json=body, headers=_SESSION)
    second = await harness.client.post("/v1/chat/completions", json=body, headers=_SESSION)

    assert first.content == second.content == b'{"from":"h2"}'
    assert hit_ports == [8001, 8002, 8002]
    assert [entry.affinity for entry in harness.log.entries] == [
        AffinityState.MISS,
        AffinityState.HIT,
    ]
