"""HeartbeatLoop tests: seq monotonicity, response surfacing, survival, auth stop."""

from __future__ import annotations

import json

import httpx
from conftest import (
    AGENT_ID,
    FIXED_NOW,
    PROTOCOL_VERSION,
    FakePreemptor,
    FakeSupervisor,
    instant_sleep,
    make_client,
    ready_replica,
    sample_metrics,
)

from fallow_agent.heartbeat import CoordinatorClient, HeartbeatLoop
from fallow_agent.idle import FakeIdleDetector
from fallow_protocol.messages import AgentState, HeartbeatResponse


def _build_loop(
    client: CoordinatorClient,
    *,
    on_response=lambda _r: None,  # type: ignore[no-untyped-def]
    on_auth_error=lambda _e: None,  # type: ignore[no-untyped-def]
    stop_after: int | None = None,
) -> HeartbeatLoop:
    loop = HeartbeatLoop(
        client=client,
        agent_id=AGENT_ID,
        protocol_version=PROTOCOL_VERSION,
        interval_s=5.0,
        preemptor=FakePreemptor(AgentState.IDLE),
        supervisor=FakeSupervisor((ready_replica(),)),
        idle=FakeIdleDetector(idle_s=3.0),
        lease_ids=lambda: ("l1",),
        metrics=sample_metrics,
        on_response=on_response,
        on_auth_error=on_auth_error,
        now=lambda: FIXED_NOW,
        sleep=instant_sleep,
    )
    if stop_after is not None:
        _install_stop_after(loop, stop_after)
    return loop


def _install_stop_after(loop: HeartbeatLoop, n: int) -> None:
    calls = {"n": 0}

    async def stopping_sleep(_seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] >= n:
            loop.request_stop()

    loop._sleep = stopping_sleep  # type: ignore[attr-defined]


async def test_seq_increments_and_state_is_reported() -> None:
    seqs: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        seqs.append(body["seq"])
        assert body["state"] == "idle"
        assert body["lease_ids"] == ["l1"]
        assert body["replicas"][0]["model_id"] == "qwen"
        return httpx.Response(200, content=HeartbeatResponse().model_dump_json())

    loop = _build_loop(make_client(handler), stop_after=3)

    await loop.run()

    assert seqs == [0, 1, 2]


async def test_response_is_surfaced_to_callback() -> None:
    surfaced: list[HeartbeatResponse] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        body = HeartbeatResponse(desired_models=("m1",))
        return httpx.Response(200, content=body.model_dump_json())

    loop = _build_loop(make_client(handler), on_response=surfaced.append, stop_after=2)

    await loop.run()

    assert len(surfaced) == 2
    assert surfaced[0].desired_models == ("m1",)


async def test_loop_survives_a_500_and_keeps_beating() -> None:
    calls = {"n": 0}
    surfaced: list[HeartbeatResponse] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500)  # transient; loop must not die
        return httpx.Response(200, content=HeartbeatResponse().model_dump_json())

    loop = _build_loop(make_client(handler), on_response=surfaced.append, stop_after=3)

    await loop.run()

    assert calls["n"] == 3  # kept beating past the failure
    assert len(surfaced) == 2  # first failed, next two succeeded
    assert loop.seq == 3  # seq advanced on every iteration including the failure


async def test_auth_error_stops_loop_and_fires_callback() -> None:
    calls = {"n": 0}
    auth_errors: list[object] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401)

    # No stop_after: only the auth error should stop the loop.
    loop = _build_loop(make_client(handler), on_auth_error=auth_errors.append)

    await loop.run()

    assert calls["n"] == 1  # stopped immediately on auth rejection
    assert len(auth_errors) == 1
