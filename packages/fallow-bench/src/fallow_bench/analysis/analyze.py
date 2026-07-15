"""Top-level reducer: run directories in, report + plots out.

``analyze`` is the module's public entry point. It is pure with respect to the
filesystem inputs and the injected ``config``/``meta`` — no wall-clock, no
randomness — so ``report.md`` and ``report.tex`` are byte-identical across runs
over the same logs (PNGs excepted).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from fallow_bench.analysis import plots
from fallow_bench.analysis.loaders import load_run
from fallow_bench.analysis.models import AnalysisConfig, MetricTable, ReportMeta, RunFrames
from fallow_bench.analysis.render import render_latex, render_markdown
from fallow_bench.analysis.table import build_table

REPORT_MD = "report.md"
REPORT_TEX = "report.tex"
TTFT_CDF = "ttft_cdf.png"
YIELD_CDF = "yield_cdf.png"
THROUGHPUT_TIMELINE = "throughput_timeline.png"


@dataclass(frozen=True)
class AnalyzeResult:
    """Paths written plus the computed table and per-arm load warnings."""

    table: MetricTable
    report_md: Path
    report_tex: Path
    plots: tuple[Path, ...]
    warnings: tuple[str, ...]


def _collect_warnings(runs: Mapping[str, RunFrames]) -> tuple[str, ...]:
    out: list[str] = []
    for arm in sorted(runs):
        out.extend(f"[{arm}] {w}" for w in runs[arm].warnings)
    return tuple(out)


def _write_plots(runs: Mapping[str, RunFrames], out_dir: Path) -> tuple[Path, ...]:
    ttft, yield_, timeline = out_dir / TTFT_CDF, out_dir / YIELD_CDF, out_dir / THROUGHPUT_TIMELINE
    plots.write_ttft_cdf(runs, ttft)
    plots.write_yield_cdf(runs, yield_)
    plots.write_throughput_timeline(runs, timeline)
    return (ttft, yield_, timeline)


def analyze(
    run_dirs: Mapping[str, Path],
    out_dir: Path,
    config: AnalysisConfig,
    meta: ReportMeta,
) -> AnalyzeResult:
    """Reduce every arm's run directory to a report + plots under ``out_dir``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    runs = {arm: load_run(run_dir, config) for arm, run_dir in run_dirs.items()}
    table = build_table(runs, config)
    precision = config.float_precision
    md_path, tex_path = out_dir / REPORT_MD, out_dir / REPORT_TEX
    md_path.write_text(render_markdown(table, meta, precision), encoding="utf-8")
    tex_path.write_text(render_latex(table, meta, precision), encoding="utf-8")
    plot_paths = _write_plots(runs, out_dir)
    return AnalyzeResult(
        table=table,
        report_md=md_path,
        report_tex=tex_path,
        plots=plot_paths,
        warnings=_collect_warnings(runs),
    )
