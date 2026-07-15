"""AgentAssembly: full wiring builds, enrolls once, and shuts down cleanly.

Exercises the composition root end-to-end with every collaborator faked/mocked:
a MockTransport coordinator, a fake idle detector and supervisor, and injected
clocks. It builds the whole agent, then drives a graceful shutdown (without
running the live loops) so the register→persist path and the final-heartbeat
teardown are covered without any spinning tasks.
"""

from __future__ import annotations

from pathlib import Path

import httpx
from main_helpers import FakeSupervisor, fixed_now, make_settings, sample_caps

from fallow_agent.idle import FakeIdleDetector
from fallow_agent.main.assembly import AgentAssembly
from fallow_agent.main.protocols import SupervisorLike
from fallow_agent.main.seams import RuntimeSeams
from fallow_agent.supervisor import SupervisorConfig
from fallow_agent.supervisor.commands import CommandFactory
from fallow_protocol.capabilities import GpuStatus
from fallow_protocol.messages import AgentConfig, HeartbeatResponse, RegisterResponse

AGENT_ID = "agent-77"
DEVICE_TOKEN = "dev-tok-77"


def _metrics() -> object:
    from fallow_agent.heartbeat import HeartbeatMetrics

    return HeartbeatMetrics(
        cpu_percent=5.0,
        mem_available_mb=16000,
        load_avg=0.2,
        gpus=(GpuStatus(index=0, vram_free_mb=8000, util_percent=1.0),),
        temp_cpu_c=40.0,
    )


class Coordinator:
    def __init__(self) -> None:
        self.register_calls = 0
        self.heartbeats = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/agents/register":
            self.register_calls += 1
            body = RegisterResponse(
                agent_id=AGENT_ID, device_token=DEVICE_TOKEN, config=AgentConfig()
            )
            return httpx.Response(200, content=body.model_dump_json())
        if path.endswith("/heartbeat"):
            self.heartbeats += 1
            return httpx.Response(200, content=HeartbeatResponse().model_dump_json())
        return httpx.Response(404)


def _seams(coordinator: Coordinator, supervisor: SupervisorLike) -> RuntimeSeams:
    def _sup(_config: SupervisorConfig, _cmd: CommandFactory) -> SupervisorLike:
        return supervisor

    async def _sleep(_s: float) -> None:
        return None

    return RuntimeSeams(
        idle_factory=FakeIdleDetector,
        http_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(coordinator.handler)
        ),
        supervisor_factory=_sup,
        caps_factory=sample_caps,
        metrics=_metrics,  # type: ignore[arg-type]
        now=fixed_now,
        monotonic=lambda: 0.0,
        sleep=_sleep,
    )


async def test_build_enrolls_and_shuts_down(tmp_path: Path) -> None:
    coordinator = Coordinator()
    log: list[str] = []
    supervisor = FakeSupervisor(log=log)
    assembly = AgentAssembly(
        make_settings(tmp_path),
        _seams(coordinator, supervisor),
        on_fatal=lambda: None,
    )

    built = await assembly.build()
    assert coordinator.register_calls == 1
    assert (tmp_path / "agent-state.json").exists()  # identity persisted

    # Graceful shutdown without starting the live loops: drives drain → final
    # heartbeat → supervisor.stop_all → sink flush through the real components.
    await built.services.stop()
    await built.aclose()

    assert coordinator.heartbeats == 1  # the single DRAINING beat
    assert "stop_all" in log  # FakeSupervisor recorded the call
