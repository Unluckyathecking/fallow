"""Tests for the C6 app wiring — config parsing + factory policy selection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from fallow_coordinator.app import CoordinatorConfig, create_app
from fallow_coordinator.scheduler import (
    CapabilityScheduler,
    ChurnAwareScheduler,
    RoundRobinScheduler,
)
from fallow_protocol.messages import AgentEvent, EventKind

ADMIN_KEY = "k"


def _config(tmp_path: Path, scheduler: str) -> CoordinatorConfig:
    return CoordinatorConfig(
        db_path=tmp_path / "coordinator.db",
        blob_dir=tmp_path / "blobs",
        unit_input_dir=tmp_path / "units",
        result_dir=tmp_path / "results",
        events_jsonl_path=tmp_path / "events.jsonl",
        gateway_log_path=tmp_path / "gateway.jsonl",
        admin_key=ADMIN_KEY,
        scheduler=scheduler,  # type: ignore[arg-type]
    )


# ── config parsing ───────────────────────────────────────────────────────────


def test_scheduler_defaults_to_capability(tmp_path: Path) -> None:
    config = _config(tmp_path, "capability")
    assert config.scheduler == "capability"
    assert config.churn_est_unit_duration_s == 60.0


@pytest.mark.parametrize("value", ["capability", "roundrobin", "churn_v2"])
def test_scheduler_accepts_the_three_arms(tmp_path: Path, value: str) -> None:
    assert _config(tmp_path, value).scheduler == value


def test_scheduler_rejects_unknown_value(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        _config(tmp_path, "bogus")


def test_churn_est_duration_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        CoordinatorConfig(
            db_path=tmp_path / "coordinator.db",
            blob_dir=tmp_path / "blobs",
            unit_input_dir=tmp_path / "units",
            result_dir=tmp_path / "results",
            events_jsonl_path=tmp_path / "events.jsonl",
            gateway_log_path=tmp_path / "gateway.jsonl",
            admin_key=ADMIN_KEY,
            churn_est_unit_duration_s=0.0,
        )


# ── factory policy selection (smoke) ─────────────────────────────────────────


def _clock() -> datetime:
    return datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def test_factory_builds_capability_by_default(tmp_path: Path) -> None:
    app = create_app(_config(tmp_path, "capability"), now=_clock)
    assert isinstance(app.state.coordinator.policy, CapabilityScheduler)


def test_factory_builds_roundrobin(tmp_path: Path) -> None:
    app = create_app(_config(tmp_path, "roundrobin"), now=_clock)
    assert isinstance(app.state.coordinator.policy, RoundRobinScheduler)


def test_factory_builds_churn_v2_with_missing_events_file(tmp_path: Path) -> None:
    # No events.jsonl on disk → empty model, no crash.
    app = create_app(_config(tmp_path, "churn_v2"), now=_clock)
    assert isinstance(app.state.coordinator.policy, ChurnAwareScheduler)


def test_factory_builds_churn_v2_from_existing_events_file(tmp_path: Path) -> None:
    config = _config(tmp_path, "churn_v2")
    start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    lines = [
        '{"malformed": true',  # skipped, no crash
        f'{{"agent_id":"a","kind":"{EventKind.USER_IDLE.value}","at":"{start.isoformat()}"}}',
        f'{{"agent_id":"a","kind":"{EventKind.USER_RETURNED.value}",'
        f'"at":"{(start + timedelta(seconds=300)).isoformat()}"}}',
    ]
    config.events_jsonl_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    app = create_app(config, now=_clock)
    assert isinstance(app.state.coordinator.policy, ChurnAwareScheduler)


async def test_factory_reads_history_without_using_run_events_as_training_data(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, "churn_v2").model_copy(
        update={"churn_history_jsonl_path": tmp_path / "history.jsonl"}
    )
    start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    history_lines = [
        "[]",
        "42",
        f'{{"agent_id":"history-agent","kind":"{EventKind.USER_IDLE.value}",'
        f'"at":"{start.isoformat()}"}}',
        f'{{"agent_id":"history-agent","kind":"{EventKind.USER_RETURNED.value}",'
        f'"at":"{(start + timedelta(seconds=300)).isoformat()}"}}',
    ]
    run_lines = [
        f'{{"agent_id":"run-agent","kind":"{EventKind.USER_IDLE.value}",'
        f'"at":"{start.isoformat()}"}}',
        f'{{"agent_id":"run-agent","kind":"{EventKind.USER_RETURNED.value}",'
        f'"at":"{(start + timedelta(seconds=10)).isoformat()}"}}',
    ]
    config.churn_history_jsonl_path.write_text("\n".join(history_lines) + "\n", encoding="utf-8")
    config.events_jsonl_path.write_text("\n".join(run_lines) + "\n", encoding="utf-8")

    app = create_app(config, now=_clock)
    policy = app.state.coordinator.policy
    assert isinstance(policy, ChurnAwareScheduler)
    assert policy._model.by_agent == {"history-agent": (300.0,)}

    await app.state.coordinator.events.write(
        AgentEvent(agent_id="new-run-agent", kind=EventKind.USER_IDLE, at=start)
    )
    assert "new-run-agent" in config.events_jsonl_path.read_text(encoding="utf-8")
    assert "new-run-agent" not in config.churn_history_jsonl_path.read_text(encoding="utf-8")
    await app.state.coordinator.client.aclose()
