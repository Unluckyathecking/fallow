"""BenchListener over a real loopback socket on an ephemeral port.

Exercises the two B2 routes with raw HTTP (no framework, no external network):
POST /simulate_input flips a FakeIdleDetector-driven PreemptController to ACTIVE
within one poll, GET /state reflects it, and an unknown route is 404.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from fallow_agent.bench import BenchIdleDetector, BenchListener
from fallow_agent.idle import FakeIdleDetector
from fallow_agent.preempt import PreemptController
from fallow_protocol.interfaces import EventSink, ProcessSupervisor
from fallow_protocol.messages import AgentConfig, AgentEvent, AgentState
from fallow_protocol.models import ModelManifest, ReplicaStatus

IDLE_LOOKS_AWAY_S = 300.0


class FakeClock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class _Supervisor(ProcessSupervisor):
    def __init__(self) -> None:
        self.calls: list[str] = []

    def start_replica(self, manifest: ModelManifest, model_path: Path, port: int) -> None: ...

    def stop_replica(self, model_id: str) -> None: ...

    def suspend_all(self) -> None:
        self.calls.append("suspend_all")

    def resume_all(self) -> None:
        self.calls.append("resume_all")

    def statuses(self) -> tuple[ReplicaStatus, ...]:
        return ()


class _Sink(EventSink):
    def emit(self, event: AgentEvent) -> None: ...


def _fixed_now() -> datetime:
    return datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)


def _controller(
    detector: BenchIdleDetector, clock: FakeClock
) -> tuple[PreemptController, _Supervisor]:
    supervisor = _Supervisor()
    controller = PreemptController(
        supervisor,
        _Sink(),
        AgentConfig(),
        agent_id="agent-bench",
        monotonic=clock,
        now=_fixed_now,
    )
    return controller, supervisor


async def _http(port: int, method: str, path: str) -> tuple[int, str]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    request = (
        f"{method} {path} HTTP/1.1\r\n"
        "Host: localhost\r\n"
        "Content-Length: 0\r\n"
        "Connection: close\r\n\r\n"
    )
    writer.write(request.encode("ascii"))
    await writer.drain()
    raw = await reader.read()
    writer.close()
    await writer.wait_closed()
    head, _, body = raw.partition(b"\r\n\r\n")
    status = int(head.split(b"\r\n")[0].decode().split(" ")[1])
    return status, body.decode()


async def test_simulate_input_flips_state_and_state_route_reflects_it() -> None:
    inner = FakeIdleDetector(idle_s=IDLE_LOOKS_AWAY_S)
    clock = FakeClock(0.0)
    detector = BenchIdleDetector(inner, monotonic=clock)
    controller, supervisor = _controller(detector, clock)
    listener = BenchListener(host="127.0.0.1", port=0, detector=detector, state_source=controller)
    port = await listener.bind()
    try:
        # Before any input the agent is IDLE.
        status, body = await _http(port, "GET", "/state")
        assert status == 200
        assert json.loads(body) == {"state": AgentState.IDLE.value, "idle_s": IDLE_LOOKS_AWAY_S}

        # Inject a synthetic return.
        status, body = await _http(port, "POST", "/simulate_input")
        assert status == 204
        assert body == ""

        # One poll observes the injected idle=0 and yields to the user.
        controller.on_poll(detector.seconds_since_input(), clock())
        assert controller.state is AgentState.ACTIVE
        assert supervisor.calls == ["suspend_all"]

        # /state now reports ACTIVE with the injected idle reading.
        status, body = await _http(port, "GET", "/state")
        assert status == 200
        assert json.loads(body) == {"state": AgentState.ACTIVE.value, "idle_s": 0.0}
    finally:
        await listener.stop()


async def test_unknown_route_is_404() -> None:
    inner = FakeIdleDetector(idle_s=1.0)
    clock = FakeClock(0.0)
    detector = BenchIdleDetector(inner, monotonic=clock)
    controller, _ = _controller(detector, clock)
    listener = BenchListener(host="127.0.0.1", port=0, detector=detector, state_source=controller)
    port = await listener.bind()
    try:
        status, _ = await _http(port, "GET", "/nope")
        assert status == 404

        status, _ = await _http(port, "DELETE", "/state")
        assert status == 404  # wrong method for a known path

        status, _ = await _http(port, "GET", "/simulate_input")
        assert status == 404  # wrong method for the POST route
    finally:
        await listener.stop()


async def test_state_route_survives_a_bound_ephemeral_port() -> None:
    inner = FakeIdleDetector(idle_s=12.0)
    clock = FakeClock(0.0)
    detector = BenchIdleDetector(inner, monotonic=clock)
    controller, _ = _controller(detector, clock)
    listener = BenchListener(host="127.0.0.1", port=0, detector=detector, state_source=controller)
    port = await listener.bind()
    assert port > 0  # ephemeral port actually allocated
    try:
        status, body = await _http(port, "GET", "/state")
        assert status == 200
        assert json.loads(body)["idle_s"] == 12.0
    finally:
        await listener.stop()
