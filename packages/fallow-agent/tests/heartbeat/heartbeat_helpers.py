"""Shared fakes and builders for module A5 (heartbeat/client) tests.

Everything is in-process: an ``httpx.MockTransport`` stands in for the
coordinator, clocks/sleeps are injected instant fakes, and hardware probes are
plain in-memory stubs. No network, no llama-server, no GPU.

Import helpers from this module (never from ``conftest``): pytest imports each
``conftest.py`` under a private internal name, so ``from conftest import X`` is
resolved by sys.path insertion order and breaks when test trees are mixed.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import httpx

from fallow_agent.heartbeat import CoordinatorClient, HeartbeatMetrics
from fallow_protocol.capabilities import DeviceCaps, GpuInfo, GpuStatus, OsFamily
from fallow_protocol.messages import AgentState
from fallow_protocol.models import ReplicaState, ReplicaStatus

BASE_URL = "http://coordinator.test"
AGENT_ID = "agent-42"
DEVICE_TOKEN = "dev-tok-abc"
ENROLLMENT_TOKEN = "enroll-xyz"
PROTOCOL_VERSION = 1
FIXED_NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)


async def instant_sleep(_seconds: float) -> None:
    return None


def make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    agent_id: str | None = AGENT_ID,
    device_token: str | None = DEVICE_TOKEN,
) -> CoordinatorClient:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return CoordinatorClient(
        base_url=BASE_URL,
        client=client,
        agent_id=agent_id,
        device_token=device_token,
        sleep=instant_sleep,
    )


def sample_caps() -> DeviceCaps:
    return DeviceCaps(
        hostname="box-1",
        os=OsFamily.LINUX,
        os_version="6.1",
        cpu_model="Test CPU",
        cpu_cores=8,
        ram_mb=32000,
        disk_free_mb=100000,
        gpus=(GpuInfo(index=0, name="RTX", vendor="nvidia", vram_mb=8000),),
        agent_version="0.1.0",
    )


def sample_metrics() -> HeartbeatMetrics:
    return HeartbeatMetrics(
        cpu_percent=12.5,
        mem_available_mb=16000,
        load_avg=0.4,
        gpus=(GpuStatus(index=0, vram_free_mb=7000, util_percent=3.0),),
        temp_cpu_c=45.0,
    )


def ready_replica(model_id: str = "qwen") -> ReplicaStatus:
    return ReplicaStatus(model_id=model_id, port=8080, state=ReplicaState.READY)


class FakePreemptor:
    """Minimal Preemptor stub exposing a settable state."""

    def __init__(self, state: AgentState = AgentState.IDLE) -> None:
        self._state = state

    def on_poll(self, idle_s: float, monotonic_now: float) -> AgentState:
        return self._state

    @property
    def state(self) -> AgentState:
        return self._state


class FakeSupervisor:
    """Minimal ProcessSupervisor stub returning a fixed status tuple."""

    def __init__(self, statuses: tuple[ReplicaStatus, ...] = ()) -> None:
        self._statuses = statuses

    def statuses(self) -> tuple[ReplicaStatus, ...]:
        return self._statuses
