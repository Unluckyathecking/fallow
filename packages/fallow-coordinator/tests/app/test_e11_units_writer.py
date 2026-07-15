"""Append-only work-unit lifecycle logging for experiment analysis."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from fallow_coordinator.app.events import UnitsWriter
from fallow_protocol.messages import UnitTransition, WorkUnitState


def test_units_writer_appends_exact_b3_records(tmp_path) -> None:
    path = tmp_path / "units.jsonl"
    writer = UnitsWriter(path)
    at = datetime(2026, 7, 15, 12, 30, tzinfo=UTC)

    writer.write(
        UnitTransition(
            work_unit_id="unit-1",
            job_id="job-1",
            agent_id="agent-a",
            attempt=1,
            state=WorkUnitState.LEASED,
            at=at,
        )
    )
    writer.write(
        UnitTransition(
            work_unit_id="unit-1",
            job_id="job-1",
            agent_id="agent-b",
            attempt=2,
            state=WorkUnitState.DONE,
            at=at + timedelta(seconds=17.25),
        )
    )

    assert [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] == [
        {
            "work_unit_id": "unit-1",
            "job_id": "job-1",
            "agent_id": "agent-a",
            "attempt": 1,
            "state": "leased",
            "t": at.timestamp(),
        },
        {
            "work_unit_id": "unit-1",
            "job_id": "job-1",
            "agent_id": "agent-b",
            "attempt": 2,
            "state": "done",
            "t": (at + timedelta(seconds=17.25)).timestamp(),
        },
    ]
