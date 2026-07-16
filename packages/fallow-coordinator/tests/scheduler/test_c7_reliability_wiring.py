"""Tests for the C7 app wiring — churn_v2 trains reliability from the history sibling.

The reliability model must read the ``units.jsonl`` sibling of the churn *history*
path (the prior run's log), never the current run's output sink. These tests put a
trap file in the run directory to prove the run's own log is not read, and drive an
end-to-end selection to prove real history actually changes ranking.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from scheduler_helpers import make_agent

from fallow_coordinator.app import CoordinatorConfig, create_app
from fallow_coordinator.scheduler import ChurnAwareScheduler
from fallow_protocol.messages import WorkUnitState

ADMIN_KEY = "k"


def _config(tmp_path: Path) -> CoordinatorConfig:
    # events (run output) and churn history live in separate directories, so the
    # run's units.jsonl and the history's units.jsonl are distinct files.
    return CoordinatorConfig(
        db_path=tmp_path / "run" / "coordinator.db",
        blob_dir=tmp_path / "blobs",
        unit_input_dir=tmp_path / "units",
        result_dir=tmp_path / "results",
        events_jsonl_path=tmp_path / "run" / "events.jsonl",
        churn_history_jsonl_path=tmp_path / "history" / "events.jsonl",
        gateway_log_path=tmp_path / "run" / "gateway.jsonl",
        admin_key=ADMIN_KEY,
        scheduler="churn_v2",
    )


def _clock() -> datetime:
    return datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _transition(agent_id: str, state: WorkUnitState) -> str:
    return (
        f'{{"work_unit_id":"u","job_id":"j","agent_id":"{agent_id}",'
        f'"attempt":1,"state":"{state.value}","at":"{_clock().isoformat()}"}}'
    )


def _write(path: Path, *lines: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_reliability_reads_history_sibling_not_run_output(tmp_path: Path) -> None:
    config = _config(tmp_path)
    # The real training data, beside the churn history.
    _write(
        config.churn_history_jsonl_path.with_name("units.jsonl"),
        "{malformed",  # skipped, no crash
        _transition("finisher", WorkUnitState.DONE),
        _transition("finisher", WorkUnitState.DONE),
        _transition("dropper", WorkUnitState.PENDING),  # requeue drop
        _transition("dropper", WorkUnitState.DEAD),  # dead drop
        _transition("dropper", WorkUnitState.LEASED),  # handout, not terminal
    )
    # A trap in the run directory: if the wiring read the run's own units.jsonl
    # instead of the history sibling, "trap" would show up in the model.
    _write(
        config.events_jsonl_path.with_name("units.jsonl"),
        _transition("trap", WorkUnitState.DONE),
    )

    policy = create_app(config, now=_clock).state.coordinator.policy
    assert isinstance(policy, ChurnAwareScheduler)
    assert policy._reliability is not None
    assert set(policy._reliability.by_agent) == {"finisher", "dropper"}
    assert "trap" not in policy._reliability.by_agent
    assert policy._reliability.success_rate("finisher") == 1.0
    assert policy._reliability.success_rate("dropper") == 0.0


def test_real_history_changes_ranking_end_to_end(tmp_path: Path) -> None:
    config = _config(tmp_path)
    # No churn history events file → empty churn model → both agents share the
    # survival prior, so idle-survival ties and reliability is the deciding signal.
    # "z" finishes its units, "a" drops them. Without reliability the tie would
    # fall to agent_id and "a" would win; reliability must flip it to "z".
    _write(
        config.churn_history_jsonl_path.with_name("units.jsonl"),
        _transition("z", WorkUnitState.DONE),
        _transition("z", WorkUnitState.DONE),
        _transition("a", WorkUnitState.DEAD),
        _transition("a", WorkUnitState.DEAD),
    )

    policy = create_app(config, now=_clock).state.coordinator.policy
    assert isinstance(policy, ChurnAwareScheduler)
    agents = [make_agent("a"), make_agent("z")]
    assert policy.select_agent("m1", needs_gpu=False, agents=agents) == "z"


def test_churn_v2_without_history_units_log_still_builds(tmp_path: Path) -> None:
    # No units.jsonl beside the history → empty reliability model, no crash, and
    # selection degrades to pure idle-survival (tie falls to agent_id).
    policy = create_app(_config(tmp_path), now=_clock).state.coordinator.policy
    assert isinstance(policy, ChurnAwareScheduler)
    assert policy._reliability is not None
    assert policy._reliability.by_agent == {}
    agents = [make_agent("a"), make_agent("z")]
    assert policy.select_agent("m1", needs_gpu=False, agents=agents) == "a"
