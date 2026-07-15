"""Contract tests for per-unit lifecycle transitions."""

from datetime import UTC, datetime

from fallow_protocol import WIRE_TYPES, UnitTransition, WorkUnitState


def test_unit_transition_is_a_public_wire_type() -> None:
    transition = UnitTransition(
        work_unit_id="unit-1",
        job_id="job-1",
        agent_id="agent-1",
        attempt=2,
        state=WorkUnitState.LEASED,
        at=datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
    )

    assert UnitTransition in WIRE_TYPES
    assert transition.model_dump(mode="json") == {
        "work_unit_id": "unit-1",
        "job_id": "job-1",
        "agent_id": "agent-1",
        "attempt": 2,
        "state": "leased",
        "at": "2026-07-15T12:00:00Z",
    }
