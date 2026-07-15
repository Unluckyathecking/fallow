"""Shared fixtures/paths for B3 analysis tests."""

from __future__ import annotations

from pathlib import Path

from fallow_bench.analysis.models import AnalysisConfig, EnergyBaseline, ReportMeta

FIXTURES = Path(__file__).parent / "fixtures"
RUN_A = FIXTURES / "run_a"
RUN_MISSING = FIXTURES / "run_missing"


def config() -> AnalysisConfig:
    """Config whose idle-baseline window matches the run_a power fixture."""
    return AnalysisConfig(energy_baseline=EnergyBaseline(start_s=0.0, end_s=5.0))


def meta() -> ReportMeta:
    return ReportMeta(title="Test run", git_sha="deadbeef", notes="fixture")
