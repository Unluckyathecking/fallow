"""Round-trip serialization tests for every wire type."""

import json
from pathlib import Path

import pytest

from fallow_protocol import (
    WIRE_TYPES,
    AgentEvent,
    Heartbeat,
    HeartbeatResponse,
    ModelManifest,
    RegisterRequest,
    WorkResult,
    WorkUnitLease,
)

FIXTURES = Path(__file__).parents[3] / "schemas/fixtures"
SAMPLES = {
    "agent_event.json": AgentEvent,
    "heartbeat.json": Heartbeat,
    "heartbeat_response.json": HeartbeatResponse,
    "model_manifest.json": ModelManifest,
    "register_request.json": RegisterRequest,
    "work_result.json": WorkResult,
    "work_unit_lease.json": WorkUnitLease,
}


def test_samples_cover_key_types():
    assert {path.name for path in FIXTURES.glob("*.json")} == set(SAMPLES)
    assert set(SAMPLES.values()) == {
        AgentEvent,
        Heartbeat,
        HeartbeatResponse,
        ModelManifest,
        RegisterRequest,
        WorkResult,
        WorkUnitLease,
    }


@pytest.mark.parametrize(("fixture", "model"), SAMPLES.items())
def test_json_roundtrip(fixture, model):
    raw = (FIXTURES / fixture).read_text()
    value = model.model_validate_json(raw)
    assert json.loads(value.model_dump_json()) == json.loads(raw)


def test_all_wire_types_are_frozen_and_strict():
    for model in WIRE_TYPES:
        assert model.model_config.get("frozen"), f"{model.__name__} must be frozen"
        assert model.model_config.get("extra") == "forbid", f"{model.__name__} must forbid extras"
