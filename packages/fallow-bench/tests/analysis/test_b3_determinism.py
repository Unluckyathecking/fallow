"""Same inputs -> byte-identical report.md and report.tex (PNGs exempt)."""

from __future__ import annotations

from pathlib import Path

from b3_helpers import RUN_A, config, meta

from fallow_bench.analysis.analyze import analyze


def test_reports_are_byte_identical(tmp_path: Path) -> None:
    runs = {"arm_b": RUN_A, "arm_a": RUN_A}  # deliberately unsorted insertion order
    out1, out2 = tmp_path / "one", tmp_path / "two"
    r1 = analyze(runs, out1, config(), meta())
    r2 = analyze(runs, out2, config(), meta())
    assert r1.report_md.read_bytes() == r2.report_md.read_bytes()
    assert r1.report_tex.read_bytes() == r2.report_tex.read_bytes()


def test_arms_sorted_regardless_of_input_order(tmp_path: Path) -> None:
    result = analyze({"zeta": RUN_A, "alpha": RUN_A}, tmp_path, config(), meta())
    assert result.table.arms == ("alpha", "zeta")
    header_line = next(
        line
        for line in result.report_md.read_text(encoding="utf-8").splitlines()
        if line.startswith("| Metric")
    )
    assert header_line == "| Metric | alpha | zeta |"
