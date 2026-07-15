"""``AgentSettings``: the agent's static, machine-local configuration.

Resolved from a TOML file with environment-variable overrides (env wins). The
result is frozen: nothing mutates it after construction. Secrets that would leak
into shell history (the enrollment token) are read from the file or env, never a
flag.

The one security-critical validation lives here and mirrors the supervisor's
(ADR 003): ``bind_host`` must never be ``0.0.0.0``. llama-server has no auth, so
binding to all interfaces would expose an open inference endpoint; v0.1 binds to
loopback or the tailnet interface only (ADR 000).
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from fallow_agent.main.errors import SettingsError
from fallow_protocol.version import __version__

# ── Defaults (no magic numbers in the logic below) ───────────────────────────
DEFAULT_STATE_PATH = Path("~/.fallow/agent-state.json")
DEFAULT_CACHE_DIR = Path("~/.fallow/models")
DEFAULT_EVENTS_PATH = Path("~/.fallow/events.jsonl")
DEFAULT_RESULTS_DIR = Path("~/.fallow/results")
DEFAULT_PORT_START = 8100
DEFAULT_PORT_COUNT = 16
DEFAULT_RECONCILE_INTERVAL_S = 5.0
DEFAULT_WORK_POLL_TIMEOUT_S = 20.0
DEFAULT_ACTIVE_SLEEP_S = 1.0
DEFAULT_BENCH_PORT = 9411  # B2 churn-injector control surface

FORBIDDEN_BIND_HOST = "0.0.0.0"  # named to reject, never to bind to

# ── Environment override keys (env beats file) ───────────────────────────────
ENV_COORDINATOR_URL = "FALLOW_COORDINATOR_URL"
ENV_ENROLLMENT_TOKEN = "FALLOW_ENROLLMENT_TOKEN"
ENV_BIND_HOST = "FALLOW_BIND_HOST"
ENV_STATE_PATH = "FALLOW_STATE_PATH"
ENV_CACHE_DIR = "FALLOW_CACHE_DIR"
ENV_EVENTS_PATH = "FALLOW_EVENTS_JSONL_PATH"
ENV_RESULTS_DIR = "FALLOW_RESULTS_DIR"
ENV_LLAMA_BINARY = "FALLOW_LLAMA_SERVER_BINARY"
ENV_PORT_START = "FALLOW_PORT_START"
ENV_PORT_COUNT = "FALLOW_PORT_COUNT"

_KNOWN_KEYS = frozenset(
    {
        "coordinator_url",
        "enrollment_token",
        "bind_host",
        "llama_server_binary",
        "state_path",
        "cache_dir",
        "events_jsonl_path",
        "results_dir",
        "reconcile_interval_s",
        "work_poll_timeout_s",
        "active_sleep_s",
        "agent_version",
        "port_range",
        "whisper",
        "bench",
    }
)


class PortRange(BaseModel):
    """Contiguous local port range replicas bind within."""

    model_config = ConfigDict(frozen=True)

    start: int = Field(default=DEFAULT_PORT_START, gt=0)
    count: int = Field(default=DEFAULT_PORT_COUNT, gt=0)


class WhisperSettings(BaseModel):
    """Passthrough tuning for the optional transcription worker.

    ``model_size_or_path`` is ``None`` when transcription is not configured; the
    assembly then leaves the ``transcribe`` worker kind out entirely.
    """

    model_config = ConfigDict(frozen=True, protected_namespaces=())

    model_size_or_path: str | None = None
    device: str = "cpu"
    compute_type: str = "int8"
    beam_size: int = Field(default=5, ge=1)


class BenchSettings(BaseModel):
    """Opt-in bench hooks for the Wave-4 churn injector (module A7/B2).

    Off by default. When ``enabled``, the assembly wraps the idle detector in a
    ``BenchIdleDetector`` and starts a ``BenchListener`` on ``port``.
    """

    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    port: int = Field(default=DEFAULT_BENCH_PORT, gt=0)


class AgentSettings(BaseModel):
    """Fully resolved, immutable agent configuration."""

    model_config = ConfigDict(frozen=True)

    coordinator_url: str
    bind_host: str
    llama_server_binary: Path
    enrollment_token: str | None = None
    state_path: Path = DEFAULT_STATE_PATH
    cache_dir: Path = DEFAULT_CACHE_DIR
    events_jsonl_path: Path = DEFAULT_EVENTS_PATH
    results_dir: Path = DEFAULT_RESULTS_DIR
    reconcile_interval_s: float = Field(default=DEFAULT_RECONCILE_INTERVAL_S, gt=0)
    work_poll_timeout_s: float = Field(default=DEFAULT_WORK_POLL_TIMEOUT_S, gt=0)
    active_sleep_s: float = Field(default=DEFAULT_ACTIVE_SLEEP_S, gt=0)
    agent_version: str = __version__
    port_range: PortRange = Field(default_factory=PortRange)
    whisper: WhisperSettings = Field(default_factory=WhisperSettings)
    bench: BenchSettings = Field(default_factory=BenchSettings)

    @field_validator("coordinator_url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        if not (value.startswith("http://") or value.startswith("https://")):
            raise ValueError("coordinator_url must start with http:// or https://")
        return value.rstrip("/")

    @field_validator("bind_host")
    @classmethod
    def _check_bind_host(cls, value: str) -> str:
        if not value:
            raise ValueError("bind_host must be set (loopback or tailnet IP)")
        if value == FORBIDDEN_BIND_HOST:
            raise ValueError(
                "bind_host must not be 0.0.0.0: llama-server has no auth; bind to "
                "loopback or the tailnet interface only"
            )
        return value


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SettingsError(f"config file not found: {path}")
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SettingsError(f"could not read config file {path}: {exc}") from exc
    unknown = set(data) - _KNOWN_KEYS
    if unknown:
        raise SettingsError(f"config file {path}: unknown key(s): {', '.join(sorted(unknown))}")
    return data


def _apply_env_overrides(data: dict[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    merged = dict(data)
    _override_scalar(merged, env, ENV_COORDINATOR_URL, "coordinator_url")
    _override_scalar(merged, env, ENV_ENROLLMENT_TOKEN, "enrollment_token")
    _override_scalar(merged, env, ENV_BIND_HOST, "bind_host")
    _override_scalar(merged, env, ENV_STATE_PATH, "state_path")
    _override_scalar(merged, env, ENV_CACHE_DIR, "cache_dir")
    _override_scalar(merged, env, ENV_EVENTS_PATH, "events_jsonl_path")
    _override_scalar(merged, env, ENV_RESULTS_DIR, "results_dir")
    _override_scalar(merged, env, ENV_LLAMA_BINARY, "llama_server_binary")
    _override_port_range(merged, env)
    return merged


def _override_scalar(
    data: dict[str, Any], env: Mapping[str, str], env_key: str, field: str
) -> None:
    value = env.get(env_key)
    if value is not None:
        data[field] = value


def _override_port_range(data: dict[str, Any], env: Mapping[str, str]) -> None:
    start = env.get(ENV_PORT_START)
    count = env.get(ENV_PORT_COUNT)
    if start is None and count is None:
        return
    current = dict(data.get("port_range") or {})
    if start is not None:
        current["start"] = _parse_int(start, ENV_PORT_START)
    if count is not None:
        current["count"] = _parse_int(count, ENV_PORT_COUNT)
    data["port_range"] = current


def _parse_int(value: str, source: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise SettingsError(f"{source} must be an integer, got {value!r}") from exc


def load_settings(config_path: Path, env: Mapping[str, str]) -> AgentSettings:
    """Load settings from ``config_path``, then apply environment overrides.

    Env variables win over file values. Raises :class:`SettingsError` on a
    missing/unreadable file, an unknown key, or a validation failure.
    """
    merged = _apply_env_overrides(_read_toml(config_path), env)
    try:
        return AgentSettings.model_validate(merged)
    except ValueError as exc:
        raise SettingsError(f"invalid agent settings: {exc}") from exc
