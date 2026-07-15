"""Tests for the shared churn-event parser (pairing + one timestamp policy)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fallow_protocol.churn_events import (
    ChurnSession,
    iter_churn_sessions,
    parse_event_at,
)

AGENT = "agent-a"
_START = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _ev(kind: str, at: object, agent_id: str = AGENT) -> dict[str, object]:
    return {"agent_id": agent_id, "kind": kind, "at": at}


# ── timestamp policy ─────────────────────────────────────────────────────────


def test_parse_accepts_z_suffix() -> None:
    assert parse_event_at("2026-01-01T12:00:00Z") == _START


def test_parse_accepts_explicit_offset() -> None:
    assert parse_event_at("2026-01-01T12:00:00+00:00") == _START


def test_parse_accepts_existing_aware_datetime() -> None:
    assert parse_event_at(_START) == _START


def test_parse_rejects_naive_string() -> None:
    assert parse_event_at("2026-01-01T12:00:00") is None


def test_parse_rejects_naive_datetime() -> None:
    assert parse_event_at(datetime(2026, 1, 1, 12, 0, 0)) is None


def test_parse_rejects_unparsable_and_non_string() -> None:
    assert parse_event_at("not-a-date") is None
    assert parse_event_at(None) is None
    assert parse_event_at(42) is None


# ── session pairing ──────────────────────────────────────────────────────────


def test_pairs_idle_and_returned() -> None:
    end = _START + timedelta(seconds=300)
    events = [_ev("user_idle", _START.isoformat()), _ev("user_returned", end.isoformat())]
    assert list(iter_churn_sessions(events)) == [ChurnSession(AGENT, _START, end)]


def test_length_s_is_end_minus_start() -> None:
    end = _START + timedelta(seconds=90)
    (session,) = iter_churn_sessions(
        [_ev("user_idle", _START.isoformat()), _ev("user_returned", end.isoformat())]
    )
    assert session.length_s == 90.0


def test_z_suffix_pairs_across_a_session() -> None:
    events = [
        _ev("user_idle", "2026-01-01T12:00:00Z"),
        _ev("user_returned", "2026-01-01T12:01:00Z"),
    ]
    (session,) = iter_churn_sessions(events)
    assert session.length_s == 60.0


def test_skips_unpaired_returned() -> None:
    assert list(iter_churn_sessions([_ev("user_returned", _START.isoformat())])) == []


def test_reopened_idle_drops_earlier_start() -> None:
    second = _START + timedelta(seconds=120)
    end = _START + timedelta(seconds=300)
    events = [
        _ev("user_idle", _START.isoformat()),
        _ev("user_idle", second.isoformat()),
        _ev("user_returned", end.isoformat()),
    ]
    (session,) = iter_churn_sessions(events)
    assert session.start == second


def test_skips_negative_length_pair() -> None:
    end = _START - timedelta(seconds=5)
    events = [_ev("user_idle", _START.isoformat()), _ev("user_returned", end.isoformat())]
    assert list(iter_churn_sessions(events)) == []


def test_naive_idle_is_skipped_so_returned_is_unpaired() -> None:
    events = [
        _ev("user_idle", "2026-01-01T12:00:00"),  # naive → rejected
        _ev("user_returned", (_START + timedelta(seconds=60)).isoformat()),
    ]
    assert list(iter_churn_sessions(events)) == []


def test_skips_malformed_events() -> None:
    events: list[dict[str, object]] = [
        {"agent_id": AGENT, "kind": "user_idle"},  # no 'at'
        {"kind": "user_returned", "at": _START.isoformat()},  # no agent_id
        {"agent_id": 1, "kind": "user_idle", "at": _START.isoformat()},  # non-str agent
    ]
    assert list(iter_churn_sessions(events)) == []


def test_pairs_are_kept_per_agent() -> None:
    end_a = _START + timedelta(seconds=100)
    end_b = _START + timedelta(seconds=200)
    events = [
        _ev("user_idle", _START.isoformat(), agent_id="a"),
        _ev("user_idle", _START.isoformat(), agent_id="b"),
        _ev("user_returned", end_b.isoformat(), agent_id="b"),
        _ev("user_returned", end_a.isoformat(), agent_id="a"),
    ]
    sessions = {s.agent_id: s.length_s for s in iter_churn_sessions(events)}
    assert sessions == {"a": 100.0, "b": 200.0}
