"""Coordinator runtime configuration (module I1).

A single frozen :class:`CoordinatorConfig` is loaded from a TOML file and then
overlaid with ``FALLOW_COORD_*`` environment variables, so nothing downstream
reads the filesystem or the process environment directly. Every tunable the app
factory needs — the one shared SQLite file, the blob / work-unit / log
directories, the admin key, the bind address, liveness thresholds, and the
chunker's units-per-batch — lives here.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Environment override prefix. ``FALLOW_COORD_ADMIN_KEY`` overrides ``admin_key``.
ENV_PREFIX = "FALLOW_COORD_"

# Defaults kept as named constants (no magic numbers buried in the model).
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8330
DEFAULT_SUSPECT_AFTER_S = 15.0
DEFAULT_OFFLINE_AFTER_S = 45.0
DEFAULT_REQUEUE_INTERVAL_S = 10.0
DEFAULT_LONG_POLL_MAX_S = 25.0
DEFAULT_POLL_SLEEP_S = 0.5
DEFAULT_CHUNKS_PER_UNIT = 32
DEFAULT_MAX_RESULT_PAYLOAD_BYTES = 64 * 1024 * 1024
DEFAULT_ADMISSION_TIMEOUT_S = 10.0
DEFAULT_ADMISSION_CAPACITY = 64
DEFAULT_AFFINITY_TTL_S = 1800.0
DEFAULT_AFFINITY_MAX = 10_000

# Scheduler policy (experiment arm): capability (arm c, v1 default), roundrobin
# (arm b), or churn_v2 (arm c v2). See ADR 011 / ADR 022.
SchedulerName = Literal["capability", "roundrobin", "churn_v2"]
DEFAULT_SCHEDULER: SchedulerName = "capability"
DEFAULT_CHURN_EST_UNIT_DURATION_S = 60.0


def _default_result_dir(validated_data: dict[str, object]) -> Path:
    db_path = validated_data["db_path"]
    if not isinstance(db_path, Path):  # pragma: no cover - pydantic validates fields in order
        raise TypeError("db_path must be validated before result_dir")
    return db_path.parent / "results"


def _default_churn_history_path(validated_data: dict[str, object]) -> Path:
    events_path = validated_data["events_jsonl_path"]
    if not isinstance(events_path, Path):  # pragma: no cover - pydantic validates fields in order
        raise TypeError("events_jsonl_path must be validated before churn_history_jsonl_path")
    return events_path


class CoordinatorConfig(BaseModel):
    """Immutable coordinator settings. Frozen so it is safe to share by reference."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Storage locations (one SQLite file shared by registry + queue).
    db_path: Path
    blob_dir: Path
    unit_input_dir: Path
    result_dir: Path = Field(default_factory=_default_result_dir)
    events_jsonl_path: Path
    # Startup-only training input. Older configs reuse the run event output.
    churn_history_jsonl_path: Path = Field(default_factory=_default_churn_history_path)
    gateway_log_path: Path

    # Secrets and networking.
    admin_key: str = Field(min_length=1)
    host: str = DEFAULT_HOST
    port: int = Field(default=DEFAULT_PORT, gt=0, le=65535)

    # Liveness thresholds handed to the registry.
    suspect_after_s: float = Field(default=DEFAULT_SUSPECT_AFTER_S, gt=0)
    offline_after_s: float = Field(default=DEFAULT_OFFLINE_AFTER_S, gt=0)

    # Background maintenance + long-poll tuning.
    requeue_interval_s: float = Field(default=DEFAULT_REQUEUE_INTERVAL_S, gt=0)
    long_poll_max_s: float = Field(default=DEFAULT_LONG_POLL_MAX_S, gt=0)
    poll_sleep_s: float = Field(default=DEFAULT_POLL_SLEEP_S, gt=0)

    # Interactive gateway waiting room.
    admission_timeout_s: float = Field(default=DEFAULT_ADMISSION_TIMEOUT_S, ge=0)
    admission_capacity: int = Field(default=DEFAULT_ADMISSION_CAPACITY, gt=0)

    # Job chunking.
    chunks_per_unit: int = Field(default=DEFAULT_CHUNKS_PER_UNIT, gt=0)

    # Agent result uploads are bounded independently from request-server limits.
    max_result_payload_bytes: int = Field(default=DEFAULT_MAX_RESULT_PAYLOAD_BYTES, gt=0)

    # Interactive session affinity is a bounded, in-memory gateway cache.
    affinity_ttl_s: float = Field(default=DEFAULT_AFFINITY_TTL_S, gt=0)
    affinity_max: int = Field(default=DEFAULT_AFFINITY_MAX, gt=0)

    # Scheduler policy selection (experiment arm) + churn-v2 survival horizon.
    scheduler: SchedulerName = DEFAULT_SCHEDULER
    churn_est_unit_duration_s: float = Field(default=DEFAULT_CHURN_EST_UNIT_DURATION_S, gt=0)


def _env_overrides() -> dict[str, str]:
    """Collect ``FALLOW_COORD_<FIELD>`` overrides as raw strings (pydantic coerces)."""
    out: dict[str, str] = {}
    for field_name in CoordinatorConfig.model_fields:
        env_name = ENV_PREFIX + field_name.upper()
        value = os.environ.get(env_name)
        if value is not None:
            out[field_name] = value
    return out


def load_config(path: str | Path) -> CoordinatorConfig:
    """Load ``path`` (TOML) and overlay ``FALLOW_COORD_*`` env overrides.

    Values from the environment win over the file; both are validated by the
    frozen model, so an unknown key or a bad type fails loudly at startup.
    """
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    merged: dict[str, object] = {**raw, **_env_overrides()}
    return CoordinatorConfig.model_validate(merged)
