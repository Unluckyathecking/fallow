"""Tests for the C7 app wiring — churn_v2 builds its reliability model from units.jsonl."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fallow_coordinator.app import CoordinatorConfig, create_app
from fallow_coordinator.scheduler import ChurnAwareScheduler
from fallow_protocol.messages import WorkUnitState

ADMIN_KEY = "k"


def _config(tmp_path: Path) -> CoordinatorConfig:
    return CoordinatorConfig(
        db_path=tmp_path / "coordinator.db",
        blob_dir=tmp_path / "blobs",
        unit_input_dir=tmp_path / "units",
        result_dir=tmp_path / "results",
        events_jsonl_path=tmp_path / "events.jsonl",
        gateway_log_path=tmp_path / "gateway.jsonl",
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


def test_churn_v2_builds_reliability_from_units_log(tmp_path: Path) -> None:
    config = _config(tmp_path)
    lines = [
        "{malformed",  # skipped, no crash
        _transition("finisher", WorkUnitState.DONE),
        _transition("finisher", WorkUnitState.DONE),
        _transition("dropper", WorkUnitState.PENDING),  # requeue drop
        _transition("dropper", WorkUnitState.DEAD),  # dead drop
        _transition("dropper", WorkUnitState.LEASED),  # handout, not terminal
    ]
    (tmp_path / "units.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    policy = create_app(config, now=_clock).state.coordinator.policy
    assert isinstance(policy, ChurnAwareScheduler)
    assert policy._reliability is not None
    assert policy._reliability.success_rate("finisher") == 1.0
    assert policy._reliability.success_rate("dropper") == 0.0


def test_churn_v2_without_units_log_still_builds(tmp_path: Path) -> None:
    # No units.jsonl on disk → empty reliability model, no crash.
    policy = create_app(_config(tmp_path), now=_clock).state.coordinator.policy
    assert isinstance(policy, ChurnAwareScheduler)
    assert policy._reliability is not None
    assert policy._reliability.by_agent == {}
