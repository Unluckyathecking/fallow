"""Module I2 — the agent runtime (composition root).

Wires every agent module (idle, preempt, supervisor, modelcache, heartbeat,
workers) into one running daemon: register-or-load identity, heartbeat uplink,
desired-model reconciliation, IDLE-gated batch work, and graceful shutdown.

Public API:

- :class:`AgentRuntime` — build, run, and gracefully stop the agent.
- :class:`AgentSettings` / :func:`load_settings` — frozen config (TOML + env).
- :class:`RuntimeSeams` — injectable construction seams (fakes in tests).
- :class:`PortAllocator` — deterministic replica port allocator.
- :func:`resolve_identity` / :class:`IdentityState` — first-run enrollment.
- :class:`AgentServices` — the start/stop lifecycle (drain → stop_all order).
- :class:`ReconcileLoop`, :class:`WorkLoop` — the two async control loops.
- :class:`AgentRuntimeError` and its subclasses — typed setup failures.
"""

from fallow_agent.main.enroll import resolve_identity
from fallow_agent.main.errors import (
    AgentRuntimeError,
    IdentityError,
    ManifestFetchError,
    PortExhaustedError,
    SettingsError,
)
from fallow_agent.main.identity import IdentityState, load_identity, save_identity
from fallow_agent.main.ports import PortAllocator
from fallow_agent.main.reconcile import ReconcileLoop
from fallow_agent.main.runtime import AgentRuntime
from fallow_agent.main.seams import RuntimeSeams
from fallow_agent.main.services import AgentServices
from fallow_agent.main.settings import (
    AgentSettings,
    BenchSettings,
    PortRange,
    WhisperSettings,
    load_settings,
)
from fallow_agent.main.work import WorkLoop

__all__ = [
    "AgentRuntime",
    "AgentRuntimeError",
    "AgentServices",
    "AgentSettings",
    "BenchSettings",
    "IdentityError",
    "IdentityState",
    "ManifestFetchError",
    "PortAllocator",
    "PortExhaustedError",
    "PortRange",
    "ReconcileLoop",
    "RuntimeSeams",
    "SettingsError",
    "WhisperSettings",
    "WorkLoop",
    "load_identity",
    "load_settings",
    "resolve_identity",
    "save_identity",
]
