"""Static configuration for the inference process supervisor.

All tunables live here so nothing is hardcoded in the launch, health, or stop
paths. The bind host defaults to loopback; production agents use a tailnet IP.
Wildcard addresses are rejected because llama-server has no authentication.
"""

from dataclasses import dataclass
from ipaddress import ip_address
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

UNSAFE_BIND_HOST_MESSAGE = (
    "bind_host must not select all interfaces: this would expose the "
    "unauthenticated llama-server; bind to loopback or a tailnet IP"
)
_WILDCARD_HOSTS = frozenset({"*", "0", "0.0", "0.0.0"})


def validate_bind_host(value: str) -> str:
    """Return a safe replica bind address or reject a wildcard address."""
    host = value.strip()
    if not host or host in _WILDCARD_HOSTS:
        raise ValueError(UNSAFE_BIND_HOST_MESSAGE)
    address_text = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        address = ip_address(address_text)
    except ValueError:
        return host
    mapped_address = getattr(address, "ipv4_mapped", None)
    if address.is_unspecified or (mapped_address is not None and mapped_address.is_unspecified):
        raise ValueError(UNSAFE_BIND_HOST_MESSAGE)
    return host


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
        object.__setattr__(self, "bind_host", validate_bind_host(self.bind_host))
