"""Injectable construction seams for the composition root.

Every piece of the outside world the runtime touches — the OS idle detector, the
HTTP client, the process supervisor, host telemetry, clocks — enters through
this one frozen bundle. Production uses the defaults; tests pass fakes so the
whole assembly runs with no network, no llama-server, and no GPU.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from fallow_agent.heartbeat import (
    HeartbeatMetrics,
    collect_caps,
    collect_heartbeat_metrics,
)
from fallow_agent.idle import create_idle_detector
from fallow_agent.main.protocols import SupervisorLike
from fallow_agent.supervisor import ChildProcessSupervisor, SupervisorConfig
from fallow_agent.supervisor.commands import CommandFactory
from fallow_agent.workers.runner import FetchInput, UploadResult
from fallow_protocol.capabilities import DeviceCaps
from fallow_protocol.interfaces import IdleDetector

IdleFactory = Callable[[], IdleDetector]
HttpClientFactory = Callable[[], httpx.AsyncClient]
SupervisorFactory = Callable[[SupervisorConfig, CommandFactory], SupervisorLike]
CapsFactory = Callable[[str], DeviceCaps]
MetricsFn = Callable[[], HeartbeatMetrics]
NowFn = Callable[[], datetime]
MonotonicFn = Callable[[], float]
SleepFn = Callable[[float], Awaitable[None]]


def _default_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient()


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class RuntimeSeams:
    """All injectable dependencies of :class:`AgentRuntime` in one place."""

    idle_factory: IdleFactory = create_idle_detector
    http_client_factory: HttpClientFactory = _default_http_client
    supervisor_factory: SupervisorFactory = ChildProcessSupervisor
    caps_factory: CapsFactory = collect_caps
    metrics: MetricsFn = collect_heartbeat_metrics
    now: NowFn = _utc_now
    monotonic: MonotonicFn = time.monotonic
    sleep: SleepFn = asyncio.sleep
    # Optional overrides for the runner's input fetch / result upload. When
    # ``None`` the assembly builds the real coordinator-backed implementations.
    fetch_input: FetchInput | None = field(default=None)
    upload: UploadResult | None = field(default=None)
