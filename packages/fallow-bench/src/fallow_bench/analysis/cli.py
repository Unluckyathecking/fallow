"""``fallow_bench analyze`` command-line front end.

Thin argument plumbing over :func:`analyze`: it builds an immutable
:class:`AnalysisConfig` + :class:`ReportMeta` from flags (or an optional JSON
config) and prints the written paths and any load warnings. All determinism-
relevant values (baseline window, metadata) are explicit inputs — nothing is read
from the clock.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from fallow_bench.analysis.analyze import analyze
from fallow_bench.analysis.models import AnalysisConfig, EnergyBaseline, ReportMeta


def _parse_runs(items: list[str]) -> dict[str, Path]:
    runs: dict[str, Path] = {}
    for item in items:
        if "=" not in item:
            raise argparse.ArgumentTypeError(f"--runs entry must be label=DIR, got {item!r}")
        label, _, raw = item.partition("=")
        if not label or not raw:
            raise argparse.ArgumentTypeError(f"--runs entry must be label=DIR, got {item!r}")
        runs[label] = Path(raw)
    return runs


def _build_config(args: argparse.Namespace) -> AnalysisConfig:
    if args.config is not None:
        data = json.loads(Path(args.config).read_text(encoding="utf-8"))
        return AnalysisConfig.model_validate(data)
    baseline = EnergyBaseline(start_s=args.baseline_start, end_s=args.baseline_end)
    return AnalysisConfig(energy_baseline=baseline, float_precision=args.precision)


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fallow_bench analyze")
    parser.add_argument("--runs", nargs="+", required=True, metavar="LABEL=DIR")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--config", default=None, help="JSON AnalysisConfig (overrides baseline)")
    parser.add_argument("--baseline-start", type=float, default=0.0)
    parser.add_argument("--baseline-end", type=float, default=0.0)
    parser.add_argument("--precision", type=int, default=3)
    parser.add_argument("--title", default="Fallow scheduling experiment")
    parser.add_argument("--git-sha", default=None)
    parser.add_argument("--notes", default=None)
    return parser


def main(argv: list[str]) -> int:
    """Run the analyze command. Returns a process exit code."""
    args = _make_parser().parse_args(argv)
    runs = _parse_runs(args.runs)
    config = _build_config(args)
    meta = ReportMeta(title=args.title, git_sha=args.git_sha, notes=args.notes)
    result = analyze(runs, args.out, config, meta)
    print(f"wrote {result.report_md}")
    print(f"wrote {result.report_tex}")
    for plot in result.plots:
        print(f"wrote {plot}")
    for warning in result.warnings:
        print(f"warning: {warning}")
    return 0
