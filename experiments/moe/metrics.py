"""Typed metrics schema for the MoE research bench (ADR 076).

A runner produces one `RunObservation` — raw counters and timings it measured
during a run — and `compute_metrics` turns that into the comparable
`BenchmarkMetrics` the bench reports. Keeping the raw observation and the derived
metrics separate means the derivation is a pure function with no model or network
in it, which is exactly the part the smoke test exercises.
"""

from __future__ import annotations

from dataclasses import dataclass

MILLION = 1_000_000
MS_PER_S = 1_000


@dataclass(frozen=True)
class RunObservation:
    """Raw counters a runner records for a single run.

    All fields are things a real runner would measure directly. The bench never
    asks a runner for a derived rate; it asks for counts and computes the rates
    itself so every runner is measured the same way.
    """

    generated_tokens: int
    wall_time_s: float
    first_token_s: float
    network_bytes: int
    expert_cache_hits: int
    expert_cache_lookups: int
    energy_wh: float


@dataclass(frozen=True)
class BenchmarkMetrics:
    """Derived, comparable metrics for one run."""

    tokens_per_sec: float
    time_to_first_token_ms: float
    per_token_network_bytes: float
    expert_cache_hit_rate: float
    watt_hours_per_million_tokens: float


def compute_metrics(obs: RunObservation) -> BenchmarkMetrics:
    """Derive `BenchmarkMetrics` from a raw observation.

    Fails fast on inputs that make a rate undefined rather than emitting a
    misleading zero. A run with no expert-cache lookups is legitimate (a
    single-machine baseline touches no remote experts), so hit rate is defined as
    0.0 there rather than treated as an error.
    """
    if obs.generated_tokens <= 0:
        raise ValueError("generated_tokens must be positive to derive per-token metrics")
    if obs.wall_time_s <= 0:
        raise ValueError("wall_time_s must be positive to derive a token rate")
    if obs.expert_cache_lookups < 0 or obs.expert_cache_hits < 0:
        raise ValueError("expert-cache counters must be non-negative")
    if obs.expert_cache_hits > obs.expert_cache_lookups:
        raise ValueError("expert_cache_hits cannot exceed expert_cache_lookups")

    hit_rate = (
        obs.expert_cache_hits / obs.expert_cache_lookups if obs.expert_cache_lookups > 0 else 0.0
    )
    return BenchmarkMetrics(
        tokens_per_sec=obs.generated_tokens / obs.wall_time_s,
        time_to_first_token_ms=obs.first_token_s * MS_PER_S,
        per_token_network_bytes=obs.network_bytes / obs.generated_tokens,
        expert_cache_hit_rate=hit_rate,
        watt_hours_per_million_tokens=obs.energy_wh / obs.generated_tokens * MILLION,
    )
