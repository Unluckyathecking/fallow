"""B3 metrics analysis: reduce a run's JSONL logs to a cross-arm report.

Public surface:

- :func:`analyze` — run directories in, ``report.md`` / ``report.tex`` / PNGs out.
- loaders (``load_run`` and the per-file functions) — tolerant JSONL → DataFrame.
- config/metadata models — :class:`AnalysisConfig`, :class:`EnergyBaseline`,
  :class:`ReportMeta`.

Every metric is a pure function of the parsed frames; every determinism-relevant
value (idle-baseline window, report metadata) is injected. See docs/adr/021.
"""

from __future__ import annotations

from fallow_bench.analysis.analyze import AnalyzeResult, analyze
from fallow_bench.analysis.loaders import (
    load_churn,
    load_client_trace,
    load_events,
    load_gateway,
    load_jobs,
    load_power,
    load_run,
)
from fallow_bench.analysis.models import (
    RUN_FILES,
    AnalysisConfig,
    EnergyBaseline,
    MetricRow,
    MetricTable,
    ReportMeta,
    RunFiles,
    RunFrames,
)
from fallow_bench.analysis.table import build_table

__all__ = [
    "RUN_FILES",
    "AnalysisConfig",
    "AnalyzeResult",
    "EnergyBaseline",
    "MetricRow",
    "MetricTable",
    "ReportMeta",
    "RunFiles",
    "RunFrames",
    "analyze",
    "build_table",
    "load_churn",
    "load_client_trace",
    "load_events",
    "load_gateway",
    "load_jobs",
    "load_power",
    "load_run",
]
