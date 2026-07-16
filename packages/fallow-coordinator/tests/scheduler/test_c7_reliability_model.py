"""Tests for the C7 reliability model — task-success tally + prior fallback."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from fallow_coordinator.scheduler.reliability import (
    DEFAULT_RELIABILITY_PRIOR,
    ReliabilityModel,
    build_reliability_model,
)
from fallow_protocol.messages import WorkUnitState


def _t(agent_id: str, state: WorkUnitState) -> dict[str, object]:
    """A minimal units.jsonl transition mapping (only the fields the model reads)."""
    return {"agent_id": agent_id, "state": state.value}


# ── success rate math ────────────────────────────────────────────────────────


def test_all_completions_is_perfect_reliability() -> None:
    model = build_reliability_model([_t("a", WorkUnitState.DONE)] * 3)
    assert model.success_rate("a") == 1.0


def test_all_drops_is_zero_reliability() -> None:
    transitions = [_t("a", WorkUnitState.PENDING), _t("a", WorkUnitState.DEAD)]
    model = build_reliability_model(transitions)
    assert model.success_rate("a") == 0.0


def test_mixed_outcomes_are_the_completed_fraction() -> None:
    transitions = [
        _t("a", WorkUnitState.DONE),
        _t("a", WorkUnitState.DONE),
        _t("a", WorkUnitState.DONE),
        _t("a", WorkUnitState.PENDING),  # requeued drop
        _t("a", WorkUnitState.DEAD),  # dead drop
    ]
    model = build_reliability_model(transitions)
    assert model.success_rate("a") == pytest.approx(3 / 5)


def test_requeue_and_dead_both_count_as_drops() -> None:
    requeued = build_reliability_model([_t("a", WorkUnitState.PENDING)])
    dead = build_reliability_model([_t("a", WorkUnitState.DEAD)])
    assert requeued.success_rate("a") == 0.0
    assert dead.success_rate("a") == 0.0


# ── LEASED and other states are not terminal ─────────────────────────────────


def test_leased_transitions_are_ignored() -> None:
    # A pure handout with no terminal outcome leaves the agent unseen → prior.
    model = build_reliability_model([_t("a", WorkUnitState.LEASED)] * 4)
    assert model.success_rate("a") == DEFAULT_RELIABILITY_PRIOR


def test_leased_does_not_dilute_a_real_outcome() -> None:
    transitions = [
        _t("a", WorkUnitState.LEASED),
        _t("a", WorkUnitState.DONE),
        _t("a", WorkUnitState.LEASED),
    ]
    model = build_reliability_model(transitions)
    assert model.success_rate("a") == 1.0  # one completion, no drops


# ── prior fallback for unseen agents ─────────────────────────────────────────


def test_unseen_agent_gets_the_optimistic_prior() -> None:
    model = build_reliability_model([_t("a", WorkUnitState.DONE)])
    assert model.success_rate("never-seen") == DEFAULT_RELIABILITY_PRIOR


def test_prior_is_configurable() -> None:
    model = build_reliability_model([], optimistic_prior=0.42)
    assert model.success_rate("anyone") == 0.42


def test_empty_log_is_all_prior() -> None:
    model = build_reliability_model([])
    assert model.by_agent == {}
    assert model.success_rate("a") == DEFAULT_RELIABILITY_PRIOR


# ── per-agent isolation ──────────────────────────────────────────────────────


def test_agents_are_tallied_independently() -> None:
    transitions = [
        _t("good", WorkUnitState.DONE),
        _t("good", WorkUnitState.DONE),
        _t("bad", WorkUnitState.DEAD),
        _t("bad", WorkUnitState.DONE),
    ]
    model = build_reliability_model(transitions)
    assert model.success_rate("good") == 1.0
    assert model.success_rate("bad") == pytest.approx(0.5)


# ── malformed rows never derail the build ────────────────────────────────────


def test_malformed_rows_are_skipped() -> None:
    transitions: list[Mapping[str, object]] = [
        {},  # no agent_id, no state
        {"state": WorkUnitState.DONE.value},  # missing agent_id
        {"agent_id": "", "state": WorkUnitState.DONE.value},  # empty agent_id
        {"agent_id": 7, "state": WorkUnitState.DONE.value},  # non-string agent_id
        {"agent_id": "a", "state": "not-a-state"},  # unknown state
        {"agent_id": "a", "state": 3},  # non-string state
        {"agent_id": "a", "state": WorkUnitState.DONE.value},  # the one good row
    ]
    model = build_reliability_model(transitions)
    assert model.by_agent == {"a": (1, 0)}
    assert model.success_rate("a") == 1.0


def test_model_can_be_built_by_hand_for_tests() -> None:
    model = ReliabilityModel(by_agent={"a": (2, 2)})
    assert model.success_rate("a") == pytest.approx(0.5)
