"""Static configuration for the inference process supervisor.

All tunables live here so nothing is hardcoded in the launch/health/stop
paths. The bind host defaults to loopback; in production it is set to the
tailnet IP. It must NEVER be 0.0.0.0 — llama-server has no authentication,
so binding to all interfaces would expose an open inference endpoint.
"""

from dataclasses import dataclass
from pathlib import Path

DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_STARTUP_TIMEOUT_S = 180.0
DEFAULT_HEALTH_POLL_INTERVAL_S = 0.5
DEFAULT_HEALTH_TIMEOUT_S = 1.0
DEFAULT_HEALTH_PATH = "/health"
DEFAULT_STOP_GRACE_S = 5.0
DEFAULT_PARALLEL = 2
DEFAULT_CONTEXT_SIZE = 8192
DEFAULT_GPU_LAYERS = 999

FORBIDDEN_BIND_HOST = "0.0.0.0"  # named to reject, never to bind to


@dataclass(frozen=True)
class SupervisorConfig:
    """Immutable supervisor configuration.

    Attributes:
        llama_binary: Path to the llama-server executable.
        bind_host: Interface replicas bind to (loopback or tailnet IP only).
        startup_timeout_s: Max time a replica may stay LOADING before it is
            killed and marked STOPPED.
        health_poll_interval_s: Delay between /health polls (also the crash
            detection granularity once a replica is READY).
        health_timeout_s: Per-request timeout for a single /health probe.
        health_path: HTTP path polled for readiness.
        stop_grace_s: Grace period after terminate() before kill().
        parallel: llama-server --parallel slot count.
        context_size: llama-server -c context size.
        gpu_layers: -ngl value used when a manifest requires VRAM.
    """

    llama_binary: Path
    bind_host: str = DEFAULT_BIND_HOST
    startup_timeout_s: float = DEFAULT_STARTUP_TIMEOUT_S
    health_poll_interval_s: float = DEFAULT_HEALTH_POLL_INTERVAL_S
    health_timeout_s: float = DEFAULT_HEALTH_TIMEOUT_S
    health_path: str = DEFAULT_HEALTH_PATH
    stop_grace_s: float = DEFAULT_STOP_GRACE_S
    parallel: int = DEFAULT_PARALLEL
    context_size: int = DEFAULT_CONTEXT_SIZE
    gpu_layers: int = DEFAULT_GPU_LAYERS

    def __post_init__(self) -> None:
        if self.bind_host == FORBIDDEN_BIND_HOST:
            raise ValueError(
                "bind_host must not be 0.0.0.0: llama-server has no auth; "
                "bind to loopback or the tailnet interface only"
            )
