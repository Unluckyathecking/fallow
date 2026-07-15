"""Marginal energy per 1000 generated tokens.

Per agent, integrate ``watts(t) - baseline`` over the run (trapezoidal, in the
power log's own time units treated as seconds) where ``baseline`` is that agent's
mean draw inside the declared idle window. Summing across agents and dividing by
client-trace tokens charges the experiment only for work-induced power above the
machine's idle floor.
"""

from __future__ import annotations

import numpy as np
import pandas as pd  # type: ignore[import-untyped]

from fallow_bench.analysis.models import EnergyBaseline

JOULES_PER_KILOTOKEN_SCALE = 1000.0


def _agent_baseline(samples: list[tuple[float, float]], window: EnergyBaseline) -> float:
    in_window = [w for t, w in samples if window.start_s <= t <= window.end_s]
    if not in_window:
        return 0.0
    return float(np.mean(np.asarray(in_window, dtype=float)))


def _agent_marginal_joules(samples: list[tuple[float, float]], window: EnergyBaseline) -> float:
    if len(samples) < 2:
        return 0.0
    ordered = sorted(samples, key=lambda tw: tw[0])
    baseline = _agent_baseline(ordered, window)
    times = np.asarray([t for t, _ in ordered], dtype=float)
    marginal = np.asarray([w - baseline for _, w in ordered], dtype=float)
    return float(np.trapezoid(marginal, x=times))


def _samples_by_agent(power: pd.DataFrame) -> dict[str, list[tuple[float, float]]]:
    by_agent: dict[str, list[tuple[float, float]]] = {}
    for row in power.to_dict("records"):
        agent, t, watts = row.get("agent_id"), row.get("t"), row.get("watts")
        if agent is None or t is None or watts is None:
            continue
        by_agent.setdefault(str(agent), []).append((float(t), float(watts)))
    return by_agent


def _total_tokens(client: pd.DataFrame) -> float:
    return float(sum(float(v) for v in client["tokens_out"].tolist() if v is not None))


def marginal_energy_per_1k_tokens(
    power: pd.DataFrame, client: pd.DataFrame, window: EnergyBaseline
) -> float | None:
    """Joules per 1000 tokens above idle baseline, or ``None`` if unmeasurable."""
    if power.empty or client.empty:
        return None
    tokens = _total_tokens(client)
    if tokens <= 0:
        return None
    total_joules = sum(
        _agent_marginal_joules(samples, window) for samples in _samples_by_agent(power).values()
    )
    return total_joules / tokens * JOULES_PER_KILOTOKEN_SCALE
