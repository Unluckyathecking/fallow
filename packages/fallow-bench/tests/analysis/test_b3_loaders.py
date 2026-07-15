"""Loaders are total: real files parse, missing files degrade to empty frames."""

from __future__ import annotations

from pathlib import Path

from b3_helpers import RUN_A, config

from fallow_bench.analysis.loaders import (
    CLIENT_COLS,
    load_churn,
    load_client_trace,
    load_run,
)


def test_load_client_trace_shape() -> None:
    frame, warnings = load_client_trace(RUN_A / "client_trace.jsonl")
    assert warnings == []
    assert list(frame.columns) == CLIENT_COLS
    assert len(frame) == 3
    assert frame["tokens_out"].tolist() == [100.0, 200.0, 300.0]


def test_load_run_all_present() -> None:
    frames = load_run(RUN_A, config())
    assert frames.warnings == ()
    assert len(frames.client) == 3
    assert len(frames.gateway) == 10
    assert len(frames.events) == 4
    assert len(frames.churn) == 2
    assert len(frames.power) == 4
    assert len(frames.jobs) == 4


def test_missing_files_are_empty_with_columns() -> None:
    frames = load_run(Path("/nonexistent/run"), config())
    assert frames.client.empty
    assert list(frames.client.columns) == CLIENT_COLS
    # Every one of the six logs reports a missing-file warning.
    assert len(frames.warnings) == 6
    assert all("missing" in w for w in frames.warnings)


def test_malformed_line_is_skipped_with_warning() -> None:
    from b3_helpers import RUN_MISSING

    frame, warnings = load_client_trace(RUN_MISSING / "client_trace.jsonl")
    assert len(frame) == 2  # two good rows, one bad line dropped
    assert any("not valid JSON" in w for w in warnings)


def test_relative_churn_time_without_epoch_origin_warns(tmp_path: Path) -> None:
    path = tmp_path / "churn.jsonl"
    path.write_text(
        '{"t_executed": 5.0, "agent": "A", "kind": "agent_kill"}\n',
        encoding="utf-8",
    )

    frame, warnings = load_churn(path)

    assert frame["t"].tolist() == [5.0]
    assert warnings == ["churn.jsonl:1 relative t_executed has no epoch origin"]
