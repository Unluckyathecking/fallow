"""Assembly wiring: the idle detector is wrapped (and a listener built) only
when [bench] is enabled.

Self-contained: a MockTransport coordinator, a fake idle detector and
supervisor, and injected clocks — no network, no llama-server, no GPU.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from fallow_agent.bench import BenchIdleDetector, BenchListener
from fallow_agent.idle import FakeIdleDetector
from fallow_agent.main.assembly import AgentAssembly
from fallow_agent.main.protocols import SupervisorLike
from fallow_agent.main.seams import RuntimeSeams
from fallow_agent.main.settings import AgentSettings, BenchSettings, PortRange
from fallow_agent.supervisor import SupervisorConfig
from fallow_agent.supervisor.commands import CommandFactory
from fallow_protocol.capabilities import DeviceCaps, GpuStatus, OsFamily
from fallow_protocol.messages import AgentConfig, HeartbeatResponse, RegisterResponse
from fallow_protocol.models import ModelManifest, ReplicaStatus

AGENT_ID = "agent-bench"
DEVICE_TOKEN = "dev-tok-bench"
BIND_HOST = "127.0.0.1"


def _fixed_now() -> datetime:
    return datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)


def _caps(_agent_version: str = "0.1.0") -> DeviceCaps:
    return DeviceCaps(
        hostname="bench-box",
        os=OsFamily.LINUX,
        os_version="6.1",
        cpu_model="Test CPU",
        cpu_cores=8,
        ram_mb=32000,
        disk_free_mb=100000,
        agent_version="0.1.0",
    )


def _metrics() -> object:
    from fallow_agent.heartbeat import HeartbeatMetrics

    return HeartbeatMetrics(
        cpu_percent=5.0,
        mem_available_mb=16000,
        load_avg=0.2,
        gpus=(GpuStatus(index=0, vram_free_mb=8000, util_percent=1.0),),
        temp_cpu_c=40.0,
    )


class _Supervisor:
    def start_replica(self, manifest: ModelManifest, model_path: Path, port: int) -> None: ...

    def stop_replica(self, model_id: str) -> None: ...

    def suspend_all(self) -> None: ...

    def resume_all(self) -> None: ...

    def stop_all(self) -> None: ...

    def statuses(self) -> tuple[ReplicaStatus, ...]:
        return ()


def _coordinator(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/v1/agents/register":
        body = RegisterResponse(agent_id=AGENT_ID, device_token=DEVICE_TOKEN, config=AgentConfig())
        return httpx.Response(200, content=body.model_dump_json())
    if path.endswith("/heartbeat"):
        return httpx.Response(200, content=HeartbeatResponse().model_dump_json())
    return httpx.Response(404)


def _seams(supervisor: SupervisorLike) -> RuntimeSeams:
    def _sup(_config: SupervisorConfig, _cmd: CommandFactory) -> SupervisorLike:
        return supervisor

    async def _sleep(_s: float) -> None:
        return None

    return RuntimeSeams(
        idle_factory=FakeIdleDetector,
        http_client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(_coordinator)),
        supervisor_factory=_sup,
        caps_factory=_caps,
        metrics=_metrics,  # type: ignore[arg-type]
        now=_fixed_now,
        monotonic=lambda: 0.0,
        sleep=_sleep,
    )


def _settings(tmp_path: Path, bench: BenchSettings) -> AgentSettings:
    return AgentSettings.model_validate(
        {
            "coordinator_url": "http://coordinator.test",
            "bind_host": BIND_HOST,
            "llama_server_binary": tmp_path / "llama-server",
            "enrollment_token": "enroll-xyz",
            "state_path": tmp_path / "agent-state.json",
            "cache_dir": tmp_path / "models",
            "events_jsonl_path": tmp_path / "events.jsonl",
            "results_dir": tmp_path / "results",
            "port_range": PortRange(start=8100, count=4),
            "bench": bench,
        }
    )


async def test_bench_enabled_wraps_detector_and_builds_listener(tmp_path: Path) -> None:
    settings = _settings(tmp_path, BenchSettings(enabled=True, port=9411))
    assembly = AgentAssembly(settings, _seams(_Supervisor()), on_fatal=lambda: None)

    built = await assembly.build()
    try:
        services = built.services
        assert isinstance(services._poll_loop._detector, BenchIdleDetector)  # type: ignore[attr-defined]
        listener = services._bench_listener  # type: ignore[attr-defined]
        assert isinstance(listener, BenchListener)
        assert listener._host == BIND_HOST  # type: ignore[attr-defined]
        assert listener._port == 9411  # type: ignore[attr-defined]
    finally:
        await built.services.stop()
        await built.aclose()


async def test_bench_disabled_leaves_raw_detector_and_no_listener(tmp_path: Path) -> None:
    settings = _settings(tmp_path, BenchSettings(enabled=False))
    assembly = AgentAssembly(settings, _seams(_Supervisor()), on_fatal=lambda: None)

    built = await assembly.build()
    try:
        services = built.services
        assert isinstance(services._poll_loop._detector, FakeIdleDetector)  # type: ignore[attr-defined]
        assert services._bench_listener is None  # type: ignore[attr-defined]
    finally:
        await built.services.stop()
        await built.aclose()


async def test_force_idle_is_wrapped_and_warns_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _settings(tmp_path, BenchSettings(enabled=True, force_idle=True))
    assembly = AgentAssembly(settings, _seams(_Supervisor()), on_fatal=lambda: None)

    with caplog.at_level(logging.WARNING, logger="fallow_agent.main.assembly"):
        built = await assembly.build()
    try:
        detector = built.services._poll_loop._detector
        assert isinstance(detector, BenchIdleDetector)
        assert detector.seconds_since_input() > 0

        detector.simulate_input()
        assert detector.seconds_since_input() == 0.0

        warnings = [record for record in caplog.records if "FORCE-IDLE ACTIVE" in record.message]
        assert len(warnings) == 1
    finally:
        await built.services.stop()
        await built.aclose()
