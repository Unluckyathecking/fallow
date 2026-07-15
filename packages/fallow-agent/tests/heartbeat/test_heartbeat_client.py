"""CoordinatorClient tests: every call path via httpx.MockTransport."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from heartbeat_helpers import (
    AGENT_ID,
    BASE_URL,
    DEVICE_TOKEN,
    ENROLLMENT_TOKEN,
    PROTOCOL_VERSION,
    instant_sleep,
    make_client,
    sample_caps,
)

from fallow_agent.heartbeat import (
    ClientRetryConfig,
    CoordinatorAuthError,
    CoordinatorClient,
    CoordinatorProtocolError,
    CoordinatorTransientError,
)
from fallow_protocol.capabilities import WorkerKind
from fallow_protocol.messages import (
    AgentConfig,
    AgentEvent,
    AgentState,
    EventKind,
    Heartbeat,
    HeartbeatResponse,
    RegisterRequest,
    RegisterResponse,
    WorkResult,
    WorkResultStatus,
    WorkUnitLease,
)


def _heartbeat() -> Heartbeat:
    return Heartbeat(
        agent_id=AGENT_ID,
        seq=0,
        sent_at=datetime(2026, 7, 15, tzinfo=UTC),
        protocol_version=PROTOCOL_VERSION,
        state=AgentState.IDLE,
        user_idle_s=1.0,
        cpu_percent=5.0,
        mem_available_mb=1000,
    )


async def test_register_stores_agent_id_and_token_and_sends_no_bearer() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["url"] = str(request.url)
        seen["body"] = request.read().decode()
        resp = RegisterResponse(agent_id=AGENT_ID, device_token=DEVICE_TOKEN, config=AgentConfig())
        return httpx.Response(200, content=resp.model_dump_json())

    client = CoordinatorClient(
        base_url=BASE_URL,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        sleep=instant_sleep,
    )
    request = RegisterRequest(
        enrollment_token=ENROLLMENT_TOKEN, protocol_version=PROTOCOL_VERSION, caps=sample_caps()
    )

    response = await client.register(request)

    assert response.agent_id == AGENT_ID
    assert client.agent_id == AGENT_ID
    assert client.device_token == DEVICE_TOKEN
    assert seen["auth"] is None  # registration carries no bearer
    assert seen["url"] == f"{BASE_URL}/v1/agents/register"
    assert ENROLLMENT_TOKEN in str(seen["body"])


async def test_heartbeat_returns_parsed_response_with_bearer() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        body = HeartbeatResponse(desired_models=("qwen",), revoked_lease_ids=("l1",))
        return httpx.Response(200, content=body.model_dump_json())

    client = make_client(handler)

    response = await client.heartbeat(_heartbeat())

    assert response.desired_models == ("qwen",)
    assert response.revoked_lease_ids == ("l1",)
    assert seen["auth"] == f"Bearer {DEVICE_TOKEN}"


async def test_poll_work_204_returns_none() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    client = make_client(handler)

    assert await client.poll_work(timeout_s=5.0) is None


async def test_poll_work_200_returns_lease_and_sends_timeout_param() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["timeout"] = request.url.params.get("timeout")
        lease = WorkUnitLease(
            work_unit_id="u1",
            job_id="j1",
            kind=WorkerKind.EMBED,
            model_id="bge",
            input_url="http://coordinator.test/input/u1",
            lease_expires=datetime(2026, 7, 15, tzinfo=UTC),
            attempt=1,
        )
        return httpx.Response(200, content=lease.model_dump_json())

    client = make_client(handler)

    lease = await client.poll_work(timeout_s=30.0)

    assert lease is not None
    assert lease.work_unit_id == "u1"
    assert seen["timeout"] == "30.0"


async def test_complete_unit_posts_to_result_path() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(204)

    client = make_client(handler)
    result = WorkResult(work_unit_id="u9", status=WorkResultStatus.SUCCEEDED)

    await client.complete_unit(result)

    assert seen["url"] == f"{BASE_URL}/v1/agents/{AGENT_ID}/work_units/u9/result"


async def test_push_event_posts_to_events_path() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(202)

    client = make_client(handler)
    event = AgentEvent(
        agent_id=AGENT_ID, kind=EventKind.USER_RETURNED, at=datetime(2026, 7, 15, tzinfo=UTC)
    )

    await client.push_event(event)

    assert seen["url"] == f"{BASE_URL}/v1/agents/{AGENT_ID}/events"


async def test_401_raises_auth_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    client = make_client(handler)

    with pytest.raises(CoordinatorAuthError):
        await client.heartbeat(_heartbeat())


async def test_5xx_raises_transient_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = make_client(handler)

    with pytest.raises(CoordinatorTransientError):
        await client.heartbeat(_heartbeat())


async def test_unexpected_4xx_raises_protocol_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(418)

    client = make_client(handler)

    with pytest.raises(CoordinatorProtocolError):
        await client.heartbeat(_heartbeat())


async def test_malformed_body_raises_protocol_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{not json")

    client = make_client(handler)

    with pytest.raises(CoordinatorProtocolError):
        await client.heartbeat(_heartbeat())


async def test_transport_error_retries_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, content=HeartbeatResponse().model_dump_json())

    client = make_client(handler)

    response = await client.heartbeat(_heartbeat())

    assert isinstance(response, HeartbeatResponse)
    assert calls["n"] == 2  # first attempt failed, retry succeeded


async def test_transport_error_exhausts_retries_then_raises_transient() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("down")

    client = CoordinatorClient(
        base_url=BASE_URL,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        agent_id=AGENT_ID,
        device_token=DEVICE_TOKEN,
        retry=ClientRetryConfig(max_retries=2),
        sleep=instant_sleep,
    )

    with pytest.raises(CoordinatorTransientError):
        await client.poll_work(timeout_s=1.0)
    assert calls["n"] == 3  # initial + 2 retries


async def test_register_is_never_retried_on_transport_error() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("nope")

    client = CoordinatorClient(
        base_url=BASE_URL,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        sleep=instant_sleep,
    )
    request = RegisterRequest(
        enrollment_token=ENROLLMENT_TOKEN, protocol_version=PROTOCOL_VERSION, caps=sample_caps()
    )

    with pytest.raises(CoordinatorTransientError):
        await client.register(request)
    assert calls["n"] == 1  # exactly one attempt, no retry


async def test_authed_call_without_token_raises_protocol_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=HeartbeatResponse().model_dump_json())

    client = CoordinatorClient(
        base_url=BASE_URL,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        agent_id=None,
        device_token=None,
        sleep=instant_sleep,
    )

    with pytest.raises(CoordinatorProtocolError):
        await client.poll_work(timeout_s=1.0)
