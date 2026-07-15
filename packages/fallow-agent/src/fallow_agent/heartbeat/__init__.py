"""Module A5 — coordinator client, event sink, and heartbeat loop.

The agent's uplink to the coordinator: registration, periodic heartbeats,
immediate event push, batch-work long-polling, and result reporting. All I/O is
injected (``httpx.AsyncClient``, clocks, sleeps, probes) so the whole module is
deterministic and network-free under test.

Public API:

- :class:`CoordinatorClient` — typed, retrying HTTP client for the coordinator.
- :class:`HttpEventSink` — non-blocking, durable (`events.jsonl`) event shipper.
- :class:`HeartbeatLoop` — the periodic heartbeat beacon.
- :func:`collect_caps` / :func:`collect_heartbeat_metrics` — host telemetry
  behind the :class:`SystemProbe` / :class:`GpuProbe` seams (with the default
  ``psutil`` / NVML implementations).
- :class:`ClientRetryConfig` / :class:`EventSinkConfig` — frozen tuning.
- :class:`CoordinatorError` hierarchy — typed auth / transient / protocol errors.
"""

from fallow_agent.heartbeat.client import CoordinatorClient
from fallow_agent.heartbeat.config import ClientRetryConfig, EventSinkConfig
from fallow_agent.heartbeat.errors import (
    CoordinatorAuthError,
    CoordinatorError,
    CoordinatorProtocolError,
    CoordinatorTransientError,
)
from fallow_agent.heartbeat.event_sink import HttpEventSink
from fallow_agent.heartbeat.loop import HeartbeatLoop
from fallow_agent.heartbeat.metrics import (
    HeartbeatMetrics,
    collect_caps,
    collect_heartbeat_metrics,
)
from fallow_agent.heartbeat.probes import (
    GpuProbe,
    NvmlGpuProbe,
    PsutilSystemProbe,
    SystemProbe,
)

__all__ = [
    "ClientRetryConfig",
    "CoordinatorAuthError",
    "CoordinatorClient",
    "CoordinatorError",
    "CoordinatorProtocolError",
    "CoordinatorTransientError",
    "EventSinkConfig",
    "GpuProbe",
    "HeartbeatLoop",
    "HeartbeatMetrics",
    "HttpEventSink",
    "NvmlGpuProbe",
    "PsutilSystemProbe",
    "SystemProbe",
    "collect_caps",
    "collect_heartbeat_metrics",
]
