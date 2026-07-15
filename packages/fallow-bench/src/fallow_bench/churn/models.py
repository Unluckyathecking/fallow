"""Frozen data models for churn schedules, config, and execution records.

Every type is a :class:`FallowModel` (immutable, unknown-field-rejecting) so a
mistyped scenario YAML fails loudly at load time instead of silently drifting.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, field_validator

from fallow_bench.churn import constants as k
from fallow_protocol.base import FallowModel


class ChurnKind(StrEnum):
    """The disruptions the injector can replay against a fleet."""

    USER_RETURN = "user_return"  # a real user touches the machine (dominant)
    AGENT_KILL = "agent_kill"  # SIGKILL/taskkill the agent (rare extra)
    NET_DROP = "net_drop"  # sever the agent's network (rare extra)


class AgentTarget(FallowModel):
    """One addressable agent running in bench mode."""

    name: str
    host: str
    bench_port: int = k.DEFAULT_BENCH_PORT


class ChurnModel(FallowModel):
    """Lognormal renewal parameters for a seeded schedule.

    ``idle_*`` govern the gap a machine sits idle before the user returns;
    ``active_*`` govern how long a session lasts. Both are the mu/sigma of the
    underlying normal (numpy ``lognormal`` convention).
    """

    idle_mu: float
    idle_sigma: float = Field(ge=0.0)
    active_mu: float
    active_sigma: float = Field(ge=0.0)
    tap_interval_s: float = k.DEFAULT_TAP_INTERVAL_S
    kill_rate_per_s: float = Field(default=k.DEFAULT_KILL_RATE_PER_S, ge=0.0)
    net_drop_rate_per_s: float = Field(default=k.DEFAULT_NET_DROP_RATE_PER_S, ge=0.0)

    @field_validator("tap_interval_s")
    @classmethod
    def _tap_positive(cls, value: float) -> float:
        if value <= 0.0:
            raise ValueError(k.POSITIVE_TAP_MSG)
        return value


class VerifyConfig(FallowModel):
    """Bounded poll of ``GET /state`` after an injected input, to time the flip."""

    enabled: bool = k.DEFAULT_VERIFY_ENABLED
    max_wait_s: float = Field(default=k.DEFAULT_VERIFY_MAX_WAIT_S, gt=0.0)
    poll_interval_s: float = Field(default=k.DEFAULT_VERIFY_POLL_S, gt=0.0)


class ChurnEvent(FallowModel):
    """One scheduled disruption at ``t_offset_s`` after the run starts."""

    t_offset_s: float = Field(ge=0.0)
    agent_name: str
    kind: ChurnKind
    params: dict[str, float] = Field(default_factory=dict)


class ChurnSection(FallowModel):
    """The churn slice of an experiment config.

    This is the seam with module B1: B1's ``ExperimentConfig`` YAML embeds this
    object under a ``churn:`` key (see open questions). ``scripted``, when given,
    is replayed verbatim and the seeded generator is bypassed.
    """

    agents: tuple[AgentTarget, ...]
    duration_s: float = Field(gt=0.0)
    seed: int
    model: ChurnModel
    commands: dict[ChurnKind, str] = Field(default_factory=dict)
    verify: VerifyConfig = Field(default_factory=VerifyConfig)
    scripted: tuple[ChurnEvent, ...] | None = None


class RunResult(FallowModel):
    """Outcome of a runner-executed shell command (kill / net-drop)."""

    ok: bool
    detail: str = ""


class ChurnRecord(FallowModel):
    """One executed event, appended to ``churn.jsonl``."""

    t: float  # epoch seconds, shared with coordinator lifecycle timestamps
    t_scheduled: float
    t_executed: float
    agent: str
    kind: ChurnKind
    ok: bool
    detail: str = ""
    flip_ms: float | None = None  # measured input→active latency (user_return only)
