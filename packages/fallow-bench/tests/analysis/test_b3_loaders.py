"""Loaders are total: real files parse, missing files degrade to empty frames."""

from __future__ import annotations

from pathlib import Path

from b3_helpers import RUN_A, config

from fallow_bench.analysis.loaders import (
    CLIENT_COLS,
    load_churn,
    load_client_trace,
    load_power,
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


def test_load_power_rebases_iso_samples_to_run_relative_seconds(tmp_path: Path) -> None:
    path = tmp_path / "power.jsonl"
    path.write_text(
        '{"t":"2026-07-15T12:00:03Z","agent_id":"A","power_w":50.0}\n',
        encoding="utf-8",
    )

    frame, warnings = load_power(path, epoch_origin_s=1784116800.0)

    assert warnings == []
    assert frame["t"].tolist() == [3.0]


def test_canonical_run_rebases_epoch_power_without_filename_warnings(tmp_path: Path) -> None:
    (tmp_path / "run_meta.json").write_text(
        '{"started_at":"2026-07-15T12:00:00Z","arm":"capability","rep":1,'
        '"seed":42,"duration_s":120.0,"git_sha":"deadbeef"}\n',
        encoding="utf-8",
    )
    (tmp_path / "client_trace.jsonl").write_text(
        '{"req_id":1,"t_submit":"2026-07-15T12:00:01Z",'
        '"t_first_token":"2026-07-15T12:00:02Z","t_done":"2026-07-15T12:00:03Z",'
        '"status":"ok","tokens_out":2}\n',
        encoding="utf-8",
    )
    (tmp_path / "power.jsonl").write_text(
        '{"t":1784116804.0,"agent_id":"A","power_w":50.0}\n',
        encoding="utf-8",
    )
    for name in ("gateway.jsonl", "events.jsonl", "churn.jsonl", "units.jsonl"):
        (tmp_path / name).write_text("", encoding="utf-8")

    frames = load_run(tmp_path, config())

    assert frames.warnings == ()
    assert frames.power["t"].tolist() == [4.0]
    assert len(frames.client) == 1


def test_load_power_preserves_legacy_relative_offsets(tmp_path: Path) -> None:
    path = tmp_path / "power.jsonl"
    path.write_text(
        '{"t":4.0,"agent_id":"A","power_w":50.0}\n',
        encoding="utf-8",
    )

    frame, warnings = load_power(path, epoch_origin_s=1784116800.0)

    assert warnings == []
    assert frame["t"].tolist() == [4.0]


def test_load_run_uses_legacy_request_trace_with_explicit_warning(tmp_path: Path) -> None:
    (tmp_path / "requests.jsonl").write_text(
        '{"req_id":1,"t_submit":1.0,"t_first_token":2.0,"t_done":3.0,'
        '"status":"ok","tokens_out":2}\n',
        encoding="utf-8",
    )
    for name in ("gateway.jsonl", "events.jsonl", "churn.jsonl", "power.jsonl", "units.jsonl"):
        (tmp_path / name).write_text("", encoding="utf-8")

    frames = load_run(tmp_path, config())

    assert len(frames.client) == 1
    assert frames.warnings == ("client_trace.jsonl missing; loaded legacy requests.jsonl",)
