"""Smoke test for the MoE bench (ADR 076).

Runs with no real model and no network. It exercises the metric derivation and
the harness plumbing against an injected fake observation, and checks that every
real runner refuses to fabricate a result.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pytest
from harness import BenchmarkConfig, run_benchmark
from metrics import RunObservation, compute_metrics
from runners import (
    ActivationCompressionRunner,
    LlamaCppRpcRunner,
    SingleMachineOffloadRunner,
    SpeculativeDecodingRunner,
)

CONFIG = BenchmarkConfig(model_ref="fake://model", prompt="hello", max_tokens=64)


@dataclass(frozen=True)
class FakeRunner:
    """A runner that returns a canned observation, standing in for a real one."""

    name: str
    observation: RunObservation

    def run(self, config: BenchmarkConfig) -> RunObservation:
        return self.observation


def test_compute_metrics_derives_expected_values() -> None:
    obs = RunObservation(
        generated_tokens=100,
        wall_time_s=5.0,
        first_token_s=0.2,
        network_bytes=200_000,
        expert_cache_hits=75,
        expert_cache_lookups=100,
        energy_wh=0.5,
    )
    m = compute_metrics(obs)
    assert m.tokens_per_sec == pytest.approx(20.0)
    assert m.time_to_first_token_ms == pytest.approx(200.0)
    assert m.per_token_network_bytes == pytest.approx(2000.0)
    assert m.expert_cache_hit_rate == pytest.approx(0.75)
    assert m.watt_hours_per_million_tokens == pytest.approx(5000.0)


def test_hit_rate_is_zero_when_no_lookups() -> None:
    obs = RunObservation(
        generated_tokens=10,
        wall_time_s=1.0,
        first_token_s=0.1,
        network_bytes=0,
        expert_cache_hits=0,
        expert_cache_lookups=0,
        energy_wh=0.01,
    )
    assert compute_metrics(obs).expert_cache_hit_rate == 0.0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("generated_tokens", 0),
        ("wall_time_s", 0.0),
    ],
)
def test_compute_metrics_rejects_undefined_rates(field: str, value: float) -> None:
    base = {
        "generated_tokens": 10,
        "wall_time_s": 1.0,
        "first_token_s": 0.1,
        "network_bytes": 100,
        "expert_cache_hits": 1,
        "expert_cache_lookups": 2,
        "energy_wh": 0.01,
    }
    base[field] = value
    with pytest.raises(ValueError, match="must be positive"):
        compute_metrics(RunObservation(**base))  # type: ignore[arg-type]


def test_compute_metrics_rejects_impossible_hit_count() -> None:
    obs = RunObservation(
        generated_tokens=10,
        wall_time_s=1.0,
        first_token_s=0.1,
        network_bytes=100,
        expert_cache_hits=5,
        expert_cache_lookups=2,
        energy_wh=0.01,
    )
    with pytest.raises(ValueError, match="cannot exceed"):
        compute_metrics(obs)


def test_run_benchmark_end_to_end_with_fake_runner() -> None:
    obs = RunObservation(
        generated_tokens=50,
        wall_time_s=2.0,
        first_token_s=0.3,
        network_bytes=50_000,
        expert_cache_hits=40,
        expert_cache_lookups=50,
        energy_wh=0.25,
    )
    result = run_benchmark(FakeRunner(name="fake", observation=obs), CONFIG)
    assert result.runner == "fake"
    assert result.metrics.tokens_per_sec == pytest.approx(25.0)
    assert math.isclose(result.metrics.expert_cache_hit_rate, 0.8)


@pytest.mark.parametrize(
    "runner",
    [
        SingleMachineOffloadRunner(),
        LlamaCppRpcRunner(),
        ActivationCompressionRunner(),
        SpeculativeDecodingRunner(),
    ],
)
def test_real_runners_refuse_to_fake_a_result(runner: object) -> None:
    with pytest.raises(NotImplementedError):
        run_benchmark(runner, CONFIG)  # type: ignore[arg-type]
