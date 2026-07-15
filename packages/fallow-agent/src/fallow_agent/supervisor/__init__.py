"""Module A3 — inference process supervisor.

Public API for owning fallow-launched inference child processes
(llama-server, faster-whisper workers): spawn, health-gate to READY,
instant suspend/resume on the preemption hot path, and graceful stop.
See the module README for invariants and lock ordering.
"""

from fallow_agent.supervisor.commands import (
    CommandFactory,
    LlamaServerCommandFactory,
    llama_server_command,
)
from fallow_agent.supervisor.config import SupervisorConfig
from fallow_agent.supervisor.health import HealthCheck, http_health_check
from fallow_agent.supervisor.supervisor import ChildProcessSupervisor

__all__ = [
    "ChildProcessSupervisor",
    "CommandFactory",
    "HealthCheck",
    "LlamaServerCommandFactory",
    "SupervisorConfig",
    "http_health_check",
    "llama_server_command",
]
