from __future__ import annotations

import asyncio
from collections.abc import Awaitable

import pytest
from gateway_helpers import ADMIN_KEY, CHAT_MODEL, buffered_handler

from fallow_coordinator.gateway import GatewayConfig
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
async def test_gateway_returns_503_immediately_when_waiting_room_is_full(build_gateway) -> None:
    sleeping = asyncio.Event()
    release = asyncio.Event()

    async def blocked_sleep(_delay: float) -> None:
        sleeping.set()
        await release.wait()

    harness = await build_gateway(
        upstream_handler=buffered_handler(b"unused"),
        endpoints={},
        config=GatewayConfig(admission_timeout_s=10, admission_capacity=1),
        monotonic=lambda: 0,
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
