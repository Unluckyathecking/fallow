"""The ``analyze`` CLI: argument plumbing and an end-to-end invocation."""

from __future__ import annotations

from pathlib import Path

import pytest
from b3_helpers import RUN_A

from fallow_bench.analysis.cli import _parse_runs, main


def test_parse_runs_ok() -> None:
    runs = _parse_runs(["a=/tmp/x", "b=/tmp/y"])
    assert runs == {"a": Path("/tmp/x"), "b": Path("/tmp/y")}


def test_parse_runs_rejects_bad_entry() -> None:
    import argparse

    with pytest.raises(argparse.ArgumentTypeError):
        _parse_runs(["noequals"])


def test_cli_writes_report(tmp_path: Path) -> None:
    out = tmp_path / "out"
    code = main(
        [
            "--runs",
            f"arm_a={RUN_A}",
            "--out",
            str(out),
            "--baseline-start",
            "0",
            "--baseline-end",
            "5",
        ]
    )
    assert code == 0
    assert (out / "report.md").exists()
    assert (out / "report.tex").exists()
    assert (out / "ttft_cdf.png").exists()
