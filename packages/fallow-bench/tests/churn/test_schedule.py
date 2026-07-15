"""Schedule determinism and scripted-loading tests."""

from __future__ import annotations

import pytest

from fallow_bench.churn import (
    AgentTarget,
    ChurnEvent,
    ChurnKind,
    ChurnModel,
    ChurnSection,
    build_schedule,
    resolve_schedule,
)


def _section(seed: int = 7, **model_kw: float) -> ChurnSection:
    return ChurnSection(
        agents=(
            AgentTarget(name="mac", host="10.0.0.1"),
            AgentTarget(name="win", host="10.0.0.2", bench_port=9500),
        ),
        duration_s=600.0,
        seed=seed,
        model=ChurnModel(idle_mu=3.0, idle_sigma=0.5, active_mu=3.0, active_sigma=0.5, **model_kw),
    )


def test_same_seed_is_byte_identical() -> None:
    a = build_schedule(_section(seed=42))
    b = build_schedule(_section(seed=42))
    assert a == b
    assert len(a) > 0


def test_different_seed_differs() -> None:
    assert build_schedule(_section(seed=1)) != build_schedule(_section(seed=2))


def test_schedule_is_sorted_and_in_window() -> None:
    schedule = build_schedule(_section())
    offsets = [e.t_offset_s for e in schedule]
    assert offsets == sorted(offsets)
    assert all(0.0 <= o <= 600.0 for o in offsets)


def test_user_return_dominates_and_extras_off_by_default() -> None:
    schedule = build_schedule(_section())
    kinds = {e.kind for e in schedule}
    assert kinds == {ChurnKind.USER_RETURN}


def test_kill_and_net_drop_appear_when_rates_positive() -> None:
    schedule = build_schedule(_section(kill_rate_per_s=0.05, net_drop_rate_per_s=0.02))
    kinds = {e.kind for e in schedule}
    assert ChurnKind.AGENT_KILL in kinds
    assert ChurnKind.NET_DROP in kinds


def test_rare_events_also_deterministic() -> None:
    kw = {"kill_rate_per_s": 0.05, "net_drop_rate_per_s": 0.02}
    assert build_schedule(_section(seed=9, **kw)) == build_schedule(_section(seed=9, **kw))


def test_tap_interval_must_be_positive() -> None:
    with pytest.raises(ValueError):
        ChurnModel(idle_mu=1.0, active_mu=1.0, tap_interval_s=0.0)


def test_resolve_prefers_scripted_over_generator() -> None:
    scripted = (
        ChurnEvent(t_offset_s=5.0, agent_name="win", kind=ChurnKind.USER_RETURN),
        ChurnEvent(t_offset_s=1.0, agent_name="mac", kind=ChurnKind.AGENT_KILL),
    )
    section = _section().model_copy(update={"scripted": scripted})
    resolved = resolve_schedule(section)
    assert [e.t_offset_s for e in resolved] == [1.0, 5.0]  # sorted, verbatim kinds
    assert resolved[0].kind is ChurnKind.AGENT_KILL
