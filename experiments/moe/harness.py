"""Harness plumbing for the MoE research bench (ADR 076).

A `Runner` measures one benchmark and returns a `RunObservation`; the harness
turns that into `BenchmarkMetrics` and pairs it with the runner name. The runner
is an injected dependency, so the plumbing runs end to end against a fake in the
smoke test with no model and no network. The real runners live in `runners.py`
and raise `NotImplementedError` until a model or fleet is actually wired up.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from metrics import BenchmarkMetrics, RunObservation, compute_metrics


@dataclass(frozen=True)
class BenchmarkConfig:
    """Inputs a runner needs to execute one run.

    Deliberately small: enough to describe a run, nothing that assumes a
    particular backend. A runner that needs more reads it from its own
    constructor, not from here.
    """

    model_ref: str
    prompt: str
    max_tokens: int


@dataclass(frozen=True)
class BenchmarkResult:
    """A named runner's derived metrics for one run."""

    runner: str
    metrics: BenchmarkMetrics


@runtime_checkable
class Runner(Protocol):
    """A benchmark runner.

    `name` labels the result; `run` executes one benchmark and returns the raw
    observation the harness derives metrics from. Real runners require a model or
    fleet and raise `NotImplementedError` from `run`.
    """

    name: str

    def run(self, config: BenchmarkConfig) -> RunObservation: ...


def run_benchmark(runner: Runner, config: BenchmarkConfig) -> BenchmarkResult:
    """Run one runner and derive its metrics."""
    observation = runner.run(config)
    return BenchmarkResult(runner=runner.name, metrics=compute_metrics(observation))
