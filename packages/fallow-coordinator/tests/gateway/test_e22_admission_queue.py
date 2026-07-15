from __future__ import annotations

import asyncio
from collections.abc import Awaitable

import httpx
import pytest
from gateway_helpers import ADMIN_KEY, CHAT_MODEL, buffered_handler, make_endpoint

from fallow_coordinator.gateway import AffinityState, GatewayConfig, LogStatus
from fallow_coordinator.gateway.admission import AdmissionQueue, AdmissionStatus


class FakeTime:
    def __init__(self) -> None:
        self.value = 0.0

    def clock(self) -> float:
        return self.value

    async def sleep(self, delay: float) -> None:
        self.value += delay
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_admission_recovers_after_two_second_outage() -> None:
    fake = FakeTime()
    queue = AdmissionQueue(
        capacity=4,
        timeout_s=10,
        poll_interval_s=0.25,
        clock=fake.clock,
        sleep=fake.sleep,
    )

    async def probe() -> str | None:
        return "replica-a" if fake.value >= 2 else None

    result = await queue.wait("chat-model", probe)

    assert result.status is AdmissionStatus.ADMITTED
    assert result.value == "replica-a"
    assert result.waited_ms == 2000


@pytest.mark.asyncio
async def test_zero_timeout_still_allows_one_immediate_probe() -> None:
    queue = AdmissionQueue(
        capacity=1,
        timeout_s=0,
        poll_interval_s=0.25,
        clock=lambda: 0,
        sleep=asyncio.sleep,
    )

    result = await queue.wait("chat-model", lambda: _return("replica-a"))

    assert result.status is AdmissionStatus.ADMITTED
    assert result.value == "replica-a"
    assert result.waited_ms == 0


@pytest.mark.asyncio
async def test_zero_timeout_probes_concurrent_requests_without_queueing() -> None:
    entered = 0
    both_entered = asyncio.Event()
    release = asyncio.Event()

    async def healthy() -> str:
        nonlocal entered
        entered += 1
        if entered == 2:
            both_entered.set()
        await release.wait()
        return "replica-a"

    queue = AdmissionQueue(
        capacity=1,
        timeout_s=0,
        poll_interval_s=0.25,
        clock=lambda: 0,
        sleep=asyncio.sleep,
    )
    first = asyncio.create_task(queue.wait("chat-model", healthy))
    second = asyncio.create_task(queue.wait("chat-model", healthy))
    await both_entered.wait()
    release.set()

    assert (await first).status is AdmissionStatus.ADMITTED
    assert (await second).status is AdmissionStatus.ADMITTED


@pytest.mark.asyncio
async def test_admission_overflow_is_immediate() -> None:
    sleeping = asyncio.Event()
    release = asyncio.Event()

    async def sleep(_delay: float) -> None:
        sleeping.set()
        await release.wait()

    async def unavailable() -> None:
        return None

    queue = AdmissionQueue(
        capacity=1,
        timeout_s=10,
        poll_interval_s=1,
        clock=lambda: 0,
        sleep=sleep,
    )
    first = asyncio.create_task(queue.wait("chat-model", unavailable))
    await sleeping.wait()

    overflow = await queue.wait("chat-model", unavailable)

    assert overflow.status is AdmissionStatus.OVERFLOW
    assert overflow.waited_ms == 0
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first


@pytest.mark.asyncio
async def test_full_waiting_room_still_probes_a_different_healthy_lane() -> None:
    sleeping = asyncio.Event()
    release = asyncio.Event()

    async def blocked_sleep(_delay: float) -> None:
        sleeping.set()
        await release.wait()

    queue = AdmissionQueue(
        capacity=1,
        timeout_s=10,
        poll_interval_s=1,
        clock=lambda: 0,
        sleep=blocked_sleep,
    )
    unavailable = asyncio.create_task(queue.wait("model-a", lambda: _return(None)))
    await sleeping.wait()

    healthy = await queue.wait("model-b", lambda: _return("replica-b"))

    assert healthy.status is AdmissionStatus.ADMITTED
    assert healthy.value == "replica-b"
    unavailable.cancel()
    with pytest.raises(asyncio.CancelledError):
        await unavailable


@pytest.mark.asyncio
async def test_admission_times_out_at_configured_deadline() -> None:
    fake = FakeTime()
    queue = AdmissionQueue(
        capacity=2,
        timeout_s=3,
        poll_interval_s=0.4,
        clock=fake.clock,
        sleep=fake.sleep,
    )

    async def unavailable() -> None:
        return None

    result = await queue.wait("chat-model", unavailable)

    assert result.status is AdmissionStatus.TIMEOUT
    assert result.value is None
    assert result.waited_ms == 3000


@pytest.mark.asyncio
async def test_admission_does_not_probe_after_an_overshot_deadline() -> None:
    now = 0.0
    probes = 0

    async def oversleep(_delay: float) -> None:
        nonlocal now
        now = 4.0

    async def late_replica() -> str | None:
        nonlocal probes
        probes += 1
        return "replica-a" if now >= 4 else None

    queue = AdmissionQueue(
        capacity=2,
        timeout_s=3,
        poll_interval_s=0.4,
        clock=lambda: now,
        sleep=oversleep,
    )

    result = await queue.wait("chat-model", late_replica)

    assert result.status is AdmissionStatus.TIMEOUT
    assert result.waited_ms == 4000
    assert probes == 1


@pytest.mark.asyncio
async def test_admission_is_fifo_within_model_lane() -> None:
    sleeps: list[asyncio.Future[None]] = []
    probes: list[str] = []
    available = False

    async def controlled_sleep(_delay: float) -> None:
        future = asyncio.get_running_loop().create_future()
        sleeps.append(future)
        await future

    def probe(name: str) -> Awaitable[str | None]:
        async def run() -> str | None:
            probes.append(name)
            return name if available else None

        return run()

    async def wait_for_sleeps(count: int) -> None:
        while len(sleeps) < count:
            await asyncio.sleep(0)

    queue = AdmissionQueue(
        capacity=2,
        timeout_s=10,
        poll_interval_s=1,
        clock=lambda: 0,
        sleep=controlled_sleep,
    )
    first = asyncio.create_task(queue.wait("chat-model", lambda: probe("first")))
    await wait_for_sleeps(1)
    second = asyncio.create_task(queue.wait("chat-model", lambda: probe("second")))
    await wait_for_sleeps(2)
    assert probes == ["first"]

    available = True
    sleeps[1].set_result(None)
    await wait_for_sleeps(3)
    assert probes == ["first"]
    sleeps[0].set_result(None)
    assert (await first).value == "first"
    sleeps[2].set_result(None)
    assert (await second).value == "second"
    assert probes == ["first", "first", "second"]


@pytest.mark.asyncio
async def test_repeated_cancellation_cannot_leak_a_ticket() -> None:
    sleeping = asyncio.Event()
    release_sleep = asyncio.Event()

    async def blocked_sleep(_delay: float) -> None:
        sleeping.set()
        await release_sleep.wait()

    queue = AdmissionQueue(
        capacity=1,
        timeout_s=10,
        poll_interval_s=1,
        clock=lambda: 0,
        sleep=blocked_sleep,
    )
    waiting = asyncio.create_task(queue.wait("chat-model", lambda: _return(None)))
    await sleeping.wait()
    await queue._lock.acquire()
    waiting.cancel()
    await asyncio.sleep(0)
    waiting.cancel()
    queue._lock.release()

    with pytest.raises(asyncio.CancelledError):
        await waiting
    admitted = await queue.wait("chat-model", lambda: _return("replica-a"))
    assert admitted.status is AdmissionStatus.ADMITTED


@pytest.mark.asyncio
async def test_gateway_returns_503_immediately_when_waiting_room_is_full(build_gateway) -> None:
    sleeping = asyncio.Event()
    release = asyncio.Event()
    now = 0.0

    async def blocked_sleep(_delay: float) -> None:
        nonlocal now
        now = 2.0
        sleeping.set()
        await release.wait()

    harness = await build_gateway(
        upstream_handler=buffered_handler(b"unused"),
        endpoints={},
        config=GatewayConfig(admission_timeout_s=10, admission_capacity=1),
        monotonic=lambda: now,
        sleep=blocked_sleep,
    )
    request = {"model": CHAT_MODEL}
    headers = {"Authorization": f"Bearer {ADMIN_KEY}"}
    first = asyncio.create_task(
        harness.client.post("/v1/chat/completions", json=request, headers=headers)
    )
    await sleeping.wait()

    overflow = await harness.client.post("/v1/chat/completions", json=request, headers=headers)

    assert overflow.status_code == 503
    assert harness.log.entries[-1].status == "shed"
    assert harness.log.entries[-1].waited_ms == 0
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    cancelled = harness.log.entries[-1]
    assert cancelled.status is LogStatus.CANCELLED
    assert cancelled.waited_ms == 2000
    assert cancelled.affinity is AffinityState.NONE


@pytest.mark.asyncio
async def test_gateway_does_not_let_a_new_arrival_bypass_a_waiter(build_gateway) -> None:
    sleeps: list[asyncio.Future[None]] = []
    endpoints: dict[str, tuple] = {CHAT_MODEL: ()}
    served = 0

    async def controlled_sleep(_delay: float) -> None:
        future = asyncio.get_running_loop().create_future()
        sleeps.append(future)
        await future

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal served
        served += 1
        return httpx.Response(200, content=b"{}")

    async def wait_for_sleeps(count: int) -> None:
        while len(sleeps) < count:
            await asyncio.sleep(0)

    harness = await build_gateway(
        upstream_handler=handler,
        endpoints=endpoints,
        config=GatewayConfig(admission_timeout_s=10, admission_capacity=2),
        monotonic=lambda: 0,
        sleep=controlled_sleep,
    )
    request = {"model": CHAT_MODEL}
    headers = {"Authorization": f"Bearer {ADMIN_KEY}"}
    first = asyncio.create_task(
        harness.client.post("/v1/chat/completions", json=request, headers=headers)
    )
    await wait_for_sleeps(1)
    endpoints[CHAT_MODEL] = (make_endpoint("h1", 8001),)
    second = asyncio.create_task(
        harness.client.post("/v1/chat/completions", json=request, headers=headers)
    )
    await wait_for_sleeps(2)
    assert served == 0
    assert not second.done()

    sleeps[0].set_result(None)
    assert (await first).status_code == 200
    sleeps[1].set_result(None)
    assert (await second).status_code == 200
    assert served == 2


@pytest.mark.asyncio
async def test_overflow_does_not_forget_an_unprobed_session(build_gateway) -> None:
    sleeping = asyncio.Event()
    release = asyncio.Event()
    endpoints: dict[str, tuple] = {CHAT_MODEL: (make_endpoint("h1", 8001),)}

    async def blocked_sleep(_delay: float) -> None:
        sleeping.set()
        await release.wait()

    harness = await build_gateway(
        upstream_handler=buffered_handler(b"{}"),
        endpoints=endpoints,
        config=GatewayConfig(admission_timeout_s=10, admission_capacity=1),
        monotonic=lambda: 0,
        sleep=blocked_sleep,
    )
    request = {"model": CHAT_MODEL}
    session_headers = {
        "Authorization": f"Bearer {ADMIN_KEY}",
        "X-Fallow-Session": "kept-session",
    }
    assert (
        await harness.client.post("/v1/chat/completions", json=request, headers=session_headers)
    ).status_code == 200

    endpoints[CHAT_MODEL] = ()
    filler = asyncio.create_task(
        harness.client.post(
            "/v1/chat/completions",
            json=request,
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        )
    )
    await sleeping.wait()
    overflow = await harness.client.post(
        "/v1/chat/completions", json=request, headers=session_headers
    )
    assert overflow.status_code == 503
    filler.cancel()
    with pytest.raises(asyncio.CancelledError):
        await filler

    endpoints[CHAT_MODEL] = (make_endpoint("h1", 8001),)
    retry = await harness.client.post("/v1/chat/completions", json=request, headers=session_headers)
    assert retry.status_code == 200
    assert harness.log.entries[-1].affinity is AffinityState.HIT


@pytest.mark.asyncio
async def test_admission_resolves_affinity_and_preserves_wait_on_served_log(build_gateway) -> None:
    fake = FakeTime()
    endpoints: dict[str, tuple] = {CHAT_MODEL: ()}

    async def recover(delay: float) -> None:
        await fake.sleep(delay)
        if fake.value >= 2:
            endpoints[CHAT_MODEL] = (make_endpoint("h1", 8001),)

    harness = await build_gateway(
        upstream_handler=buffered_handler(b'{"ok":true}'),
        endpoints=endpoints,
        config=GatewayConfig(admission_timeout_s=10, admission_poll_interval_s=0.25),
        monotonic=fake.clock,
        sleep=recover,
    )
    response = await harness.client.post(
        "/v1/chat/completions",
        json={"model": CHAT_MODEL},
        headers={
            "Authorization": f"Bearer {ADMIN_KEY}",
            "X-Fallow-Session": "session-a",
        },
    )

    assert response.status_code == 200
    entry = harness.log.entries[-1]
    assert entry.status is LogStatus.SERVED
    assert entry.waited_ms == 2000
    assert entry.affinity is AffinityState.MISS


@pytest.mark.asyncio
async def test_upstream_error_preserves_affinity_and_admission_wait(build_gateway) -> None:
    fake = FakeTime()
    endpoints: dict[str, tuple] = {CHAT_MODEL: ()}

    async def recover(delay: float) -> None:
        await fake.sleep(delay)
        if fake.value >= 2:
            endpoints[CHAT_MODEL] = (
                make_endpoint("h1", 8001),
                make_endpoint("h2", 8002, agent_id="agent-2"),
            )

    def unavailable(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    harness = await build_gateway(
        upstream_handler=unavailable,
        endpoints=endpoints,
        config=GatewayConfig(admission_timeout_s=10, admission_poll_interval_s=0.25),
        monotonic=fake.clock,
        sleep=recover,
    )
    response = await harness.client.post(
        "/v1/chat/completions",
        json={"model": CHAT_MODEL},
        headers={
            "Authorization": f"Bearer {ADMIN_KEY}",
            "X-Fallow-Session": "session-a",
        },
    )

    assert response.status_code == 502
    entry = harness.log.entries[-1]
    assert entry.status is LogStatus.ERROR
    assert entry.waited_ms == 2000
    assert entry.affinity is AffinityState.MISS


async def _return[T](value: T) -> T:
    return value
