"""Marginal-energy math: fixture value plus an independent hand integral."""

from __future__ import annotations

import pandas as pd  # type: ignore[import-untyped]
import pytest
from b3_helpers import RUN_A, config

from fallow_bench.analysis.energy import marginal_energy_per_1k_tokens
from fallow_bench.analysis.loaders import load_run
from fallow_bench.analysis.models import EnergyBaseline


def test_energy_fixture_value() -> None:
    frames = load_run(RUN_A, config())
    # baseline (window [0,5]) = 50 W; marginal integral = 1050 J; tokens = 600.
    # 1050 / 600 * 1000 = 1750 J / 1k tokens.
    value = marginal_energy_per_1k_tokens(frames.power, frames.client, config().energy_baseline)
    assert value == pytest.approx(1750.0)


def test_energy_hand_integral() -> None:
    # Two agents, flat baseline 100 W each in [0,10]; agent X spikes to 300 W.
    power = pd.DataFrame(
        [
            {"t": 0.0, "agent_id": "X", "watts": 100.0},
            {"t": 10.0, "agent_id": "X", "watts": 100.0},
            {"t": 20.0, "agent_id": "X", "watts": 300.0},
            {"t": 30.0, "agent_id": "X", "watts": 300.0},
            {"t": 0.0, "agent_id": "Y", "watts": 100.0},
            {"t": 30.0, "agent_id": "Y", "watts": 100.0},
        ]
    )
    client = pd.DataFrame([{"tokens_out": 2000.0}])
    window = EnergyBaseline(start_s=0.0, end_s=10.0)
    # X marginal: seg[0,10]=0, seg[10,20]=avg(0,200)*10=1000, seg[20,30]=200*10=2000 -> 3000 J.
    # Y marginal: 0. Total 3000 J / 2000 tokens * 1000 = 1500.
    assert marginal_energy_per_1k_tokens(power, client, window) == pytest.approx(1500.0)


def test_energy_none_without_tokens() -> None:
    power = pd.DataFrame([{"t": 0.0, "agent_id": "X", "watts": 100.0}])
    client = pd.DataFrame([{"tokens_out": 0.0}])
    window = EnergyBaseline(start_s=0.0, end_s=10.0)
    assert marginal_energy_per_1k_tokens(power, client, window) is None
