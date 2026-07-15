"""Tests for the C6 churn model — survival math + event pairing (build)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from fallow_coordinator.scheduler.churn_model import (
    DEFAULT_OPTIMISTIC_PRIOR,
    ChurnModel,
    build_churn_model,
)
from fallow_protocol.messages import EventKind

AGENT = "a"


def _hour_of(moment: datetime) -> int:
    return moment.hour


def _model(
    bucket: dict[tuple[str, int], tuple[float, ...]],
    pool: dict[str, tuple[float, ...]],
    prior: float = DEFAULT_OPTIMISTIC_PRIOR,
) -> ChurnModel:
    return ChurnModel(by_bucket=bucket, by_agent=pool, optimistic_prior=prior)


# ── survival math over a hand-built dense bucket ─────────────────────────────

_DENSE = (10.0, 20.0, 30.0, 40.0, 50.0)  # 5 samples → bucket used directly


def test_survival_unconditioned_fraction() -> None:
    model = _model({(AGENT, 12): _DENSE}, {AGENT: _DENSE})
    # count>=0 == 5, count>=25 == {30,40,50} == 3.
    assert model.survival(AGENT, 12, idle_age_s=0.0, horizon_s=25.0) == pytest.approx(3 / 5)


def test_survival_conditions_on_current_idle_age() -> None:
    model = _model({(AGENT, 12): _DENSE}, {AGENT: _DENSE})
    # Already idle 20s: denom = count>=20 == {20,30,40,50} == 4;
    # numer = count>=35 == {40,50} == 2 → 0.5.
    assert model.survival(AGENT, 12, idle_age_s=20.0, horizon_s=15.0) == pytest.approx(0.5)


def test_non_positive_horizon_is_certain() -> None:
    model = _model({(AGENT, 12): _DENSE}, {AGENT: _DENSE})
    assert model.survival(AGENT, 12, idle_age_s=20.0, horizon_s=0.0) == 1.0
    assert model.survival(AGENT, 12, idle_age_s=20.0, horizon_s=-5.0) == 1.0


def test_conditioning_age_beyond_all_samples_falls_to_prior() -> None:
    model = _model({(AGENT, 12): _DENSE}, {AGENT: _DENSE}, prior=0.42)
    # No session ever reached 100s idle → no evidence → prior.
    assert model.survival(AGENT, 12, idle_age_s=100.0, horizon_s=10.0) == 0.42


# ── sparse-bucket fallbacks ──────────────────────────────────────────────────


def test_thin_bucket_falls_back_to_all_hours_pool() -> None:
    thin_bucket = {(AGENT, 9): (10.0, 20.0)}  # < MIN_BUCKET_SAMPLES
    pool = {AGENT: (1.0, 2.0, 3.0, 10.0, 20.0, 100.0)}  # 6 samples
    model = _model(thin_bucket, pool)
    # Pool used, not the 2-sample bucket: count>=0 == 6, count>=50 == {100} == 1.
    assert model.survival(AGENT, 9, idle_age_s=0.0, horizon_s=50.0) == pytest.approx(1 / 6)


def test_unknown_agent_falls_back_to_prior() -> None:
    model = _model({}, {}, prior=0.77)
    assert model.survival("ghost", 3, idle_age_s=0.0, horizon_s=30.0) == 0.77


def test_empty_pool_for_known_key_still_uses_prior() -> None:
    model = _model({(AGENT, 9): (10.0,)}, {AGENT: ()}, prior=0.55)
    # Bucket is thin (1<5) and pool is empty → prior.
    assert model.survival(AGENT, 9, idle_age_s=0.0, horizon_s=5.0) == 0.55


# ── build_churn_model: pairing over event mappings ───────────────────────────

_START = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _ev(agent_id: str, kind: EventKind, at: datetime) -> dict[str, object]:
    return {"agent_id": agent_id, "kind": kind.value, "at": at.isoformat()}


def _pair(agent_id: str, start: datetime, length_s: float) -> list[dict[str, object]]:
    end = start + timedelta(seconds=length_s)
    return [
        _ev(agent_id, EventKind.USER_IDLE, start),
        _ev(agent_id, EventKind.USER_RETURNED, end),
    ]


def test_build_pairs_idle_and_returned_into_a_session() -> None:
    model = build_churn_model(_pair(AGENT, _START, 300.0), _hour_of)
    assert model.by_agent[AGENT] == (300.0,)
    assert model.by_bucket[(AGENT, 12)] == (300.0,)


def test_build_skips_unpaired_returned() -> None:
    events = [_ev(AGENT, EventKind.USER_RETURNED, _START)]
    model = build_churn_model(events, _hour_of)
    assert model.by_agent == {}


def test_build_reopened_idle_drops_the_earlier_start() -> None:
    events = [
        _ev(AGENT, EventKind.USER_IDLE, _START),
        _ev(AGENT, EventKind.USER_IDLE, _START + timedelta(seconds=120)),
        _ev(AGENT, EventKind.USER_RETURNED, _START + timedelta(seconds=300)),
    ]
    model = build_churn_model(events, _hour_of)
    # Pairs with the 2nd idle (300-120=180s), not the first.
    assert model.by_agent[AGENT] == (180.0,)


def test_build_skips_negative_length_pair() -> None:
    events = [
        _ev(AGENT, EventKind.USER_IDLE, _START + timedelta(seconds=300)),
        _ev(AGENT, EventKind.USER_RETURNED, _START),
    ]
    model = build_churn_model(events, _hour_of)
    assert model.by_agent == {}


def test_build_skips_malformed_lines() -> None:
    events: list[dict[str, object]] = [
        {"agent_id": AGENT, "kind": "user_idle"},  # no 'at'
        {"kind": "user_returned", "at": _START.isoformat()},  # no agent_id
        {"agent_id": AGENT, "kind": "user_idle", "at": "not-a-date"},
    ]
    model = build_churn_model(events, _hour_of)
    assert model.by_agent == {}


def test_build_accepts_z_suffix_and_datetime_at() -> None:
    events = [
        {"agent_id": AGENT, "kind": "user_idle", "at": "2026-01-01T12:00:00Z"},
        {"agent_id": AGENT, "kind": "user_returned", "at": _START + timedelta(seconds=60)},
    ]
    model = build_churn_model(events, _hour_of)
    assert model.by_agent[AGENT] == (60.0,)


def test_hour_bucketing_separates_distributions() -> None:
    events: list[dict[str, object]] = []
    for i in range(5):
        events += _pair(AGENT, _START.replace(hour=9) + timedelta(minutes=i), 100.0)
    for i in range(5):
        events += _pair(AGENT, _START.replace(hour=14) + timedelta(minutes=i), 1000.0)
    model = build_churn_model(events, _hour_of)
    # Distinct dense buckets → different survival for the same query at each hour.
    assert model.survival(AGENT, 9, idle_age_s=0.0, horizon_s=500.0) == 0.0
    assert model.survival(AGENT, 14, idle_age_s=0.0, horizon_s=500.0) == 1.0
