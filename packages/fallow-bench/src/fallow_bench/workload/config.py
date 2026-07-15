"""Experiment configuration schema and YAML loader (module B1).

``ExperimentConfig`` is the single source of truth for one arm's run. It is a
frozen ``FallowModel`` (``extra="forbid"``) so a typo in the YAML fails loudly
instead of being silently ignored. Secrets are referenced by env-var *name*
only; the values are read at the ``__main__`` boundary.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from pydantic import Field, ValidationError

from fallow_protocol import FallowModel, WorkerKind


class InteractiveConfig(FallowModel):
    """Open-loop interactive load parameters."""

    rate_per_min: float = Field(gt=0)  # mean arrivals per minute (Poisson)
    max_tokens: int = Field(gt=0)
    prompt_files: tuple[str, ...] = Field(min_length=1)
    request_timeout_s: float = Field(default=60.0, gt=0)


class BatchConfig(FallowModel):
    """The single batch job submitted mid-run via the admin API."""

    corpus_path: str  # becomes JobSubmit.payload_ref
    submit_at_s: float = Field(ge=0)
    kind: WorkerKind = WorkerKind.EMBED
    model_id: str  # model the batch job runs against
    poll_interval_s: float = Field(default=10.0, gt=0)
    priority: int = 0


class SamplingConfig(FallowModel):
    """Admin-API sampling for the power/state energy trace."""

    admin_poll_hz: float = Field(default=1.0, gt=0)
    admin_key_env: str  # env var holding the admin key


class ExperimentConfig(FallowModel):
    """A complete, self-contained description of one experiment arm."""

    arm_label: str
    coordinator_url: str
    api_key_env: str  # env var holding the client API key
    model_id: str  # interactive model
    duration_s: float = Field(gt=0)
    seed: int
    interactive: InteractiveConfig
    batch: BatchConfig
    sampling: SamplingConfig
    # Optional churn slice (module B2) embedded in the same shared YAML; B1
    # ignores it but must not reject it under extra="forbid".
    churn: dict[str, object] | None = None


def load_config(path: Path) -> ExperimentConfig:
    """Load and validate an experiment config from a YAML file."""
    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a YAML mapping at the top level")
    try:
        return ExperimentConfig.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"{path}: invalid experiment config\n{exc}") from exc
