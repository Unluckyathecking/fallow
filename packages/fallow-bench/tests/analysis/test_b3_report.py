"""End-to-end analyze: report content, plots, and graceful missing-file rows."""

from __future__ import annotations

from pathlib import Path

from b3_helpers import RUN_A, RUN_MISSING, config, meta

from fallow_bench.analysis.analyze import analyze


def test_report_contents(tmp_path: Path) -> None:
    result = analyze({"arm_a": RUN_A, "arm_b": RUN_A}, tmp_path, config(), meta())
    md = result.report_md.read_text(encoding="utf-8")
    assert "| Metric | arm_a | arm_b |" in md
    assert "Commit: `deadbeef`" in md
    assert "0.200" in md  # TTFT p50
    assert "80.000" in md  # % served on-prem
    assert "1750.000" in md  # marginal energy
    assert result.warnings == ()


def test_all_outputs_written(tmp_path: Path) -> None:
    result = analyze({"arm_a": RUN_A}, tmp_path, config(), meta())
    assert result.report_md.exists()
    assert result.report_tex.exists()
    for plot in result.plots:
        assert plot.exists()
        assert plot.suffix == ".png"
    tex = result.report_tex.read_text(encoding="utf-8")
    assert "\\begin{tabular}" in tex
    assert "\\%" in tex  # percent sign escaped for LaTeX


def test_missing_files_render_em_dash(tmp_path: Path) -> None:
    result = analyze({"m": RUN_MISSING}, tmp_path, config(), meta())
    md = result.report_md.read_text(encoding="utf-8")
    assert "—" in md  # energy / recovery / units-per-hour unmeasurable
    assert "50.000" in md  # % served still computed (1 of 2)
    assert any("missing" in w for w in result.warnings)
