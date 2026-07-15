"""Tests for ChurnAwareScheduler (arm c v2) — churn ranking, eligibility, replicas."""

from __future__ import annotations

from collections.abc import Mapping

from scheduler_helpers import make_agent, make_endpoint, make_replica_status

from fallow_coordinator.scheduler.churn_model import ChurnModel
from fallow_coordinator.scheduler.v2 import ChurnAwareScheduler
from fallow_protocol.messages import AgentSnapshot, AgentState
from fallow_protocol.models import ReplicaState

FIXED_HOUR = 12


def _model(
    pool: Mapping[str, tuple[float, ...]],
    bucket: Mapping[tuple[str, int], tuple[float, ...]] | None = None,
    prior: float = 0.9,
) -> ChurnModel:
    return ChurnModel(by_bucket=dict(bucket or {}), by_agent=dict(pool), optimistic_prior=prior)


def _sched(model: ChurnModel, hour: int = FIXED_HOUR, est: float = 60.0) -> ChurnAwareScheduler:
    return ChurnAwareScheduler(model, est, hour_fn=lambda: hour)


def _idle(agent_id: str, *, user_idle_s: float = 0.0, **kw: object) -> AgentSnapshot:
    return make_agent(agent_id, **kw).model_copy(update={"user_idle_s": user_idle_s})  # type: ignore[arg-type]


def test_prefers_agent_with_longer_expected_idleness() -> None:
    # a: every session outlives the 60s horizon; b: none do.
    model = _model({"a": (100.0,) * 5, "b": (30.0,) * 5})
    agents = [_idle("a"), _idle("b")]
    assert _sched(model).select_agent("m1", needs_gpu=False, agents=agents) == "a"


def test_conditions_on_current_idle_age() -> None:
    # Same pool for both; the agent already idle longer has higher conditional
    # survival over the next 60s, so it wins purely on tenure.
    shared = (10.0, 20.0, 30.0, 300.0, 400.0, 500.0)
    model = _model({"a": shared, "b": shared})
    agents = [_idle("a", user_idle_s=0.0), _idle("b", user_idle_s=300.0)]
    assert _sched(model).select_agent("m1", needs_gpu=False, agents=agents) == "b"


def test_eligibility_gate_beats_churn_score() -> None:
    # z has a perfect churn score but is ACTIVE (ineligible); b is eligible but weak.
    model = _model({"z": (1000.0,) * 5, "b": (1.0,) * 5})
    agents = [_idle("z", state=AgentState.ACTIVE), _idle("b")]
    assert _sched(model).select_agent("m1", needs_gpu=False, agents=agents) == "b"


def test_returns_none_when_nobody_eligible() -> None:
    model = _model({"a": (1000.0,) * 5})
    agents = [_idle("a", state=AgentState.ACTIVE), _idle("s", suspect=True)]
    assert _sched(model).select_agent("m1", needs_gpu=False, agents=agents) is None
    assert _sched(model).select_agent("m1", needs_gpu=False, agents=[]) is None


def test_needs_gpu_excludes_gpuless_agents() -> None:
    model = _model({"cpu": (1000.0,) * 5, "gpu": (1.0,) * 5})
    agents = [_idle("cpu", gpu_count=0), _idle("gpu", gpu_count=1)]
    # cpu's churn score is higher, but needs_gpu filters it out first.
    assert _sched(model).select_agent("m1", needs_gpu=True, agents=agents) == "gpu"


def test_hour_switches_the_winner() -> None:
    bucket = {
        ("a", 9): (1000.0,) * 5,
        ("a", 14): (1.0,) * 5,
        ("b", 9): (1.0,) * 5,
        ("b", 14): (1000.0,) * 5,
    }
    model = _model({"a": (1.0,) * 10, "b": (1.0,) * 10}, bucket=bucket)
    agents = [_idle("a"), _idle("b")]
    assert _sched(model, hour=9).select_agent("m1", needs_gpu=False, agents=agents) == "a"
    assert _sched(model, hour=14).select_agent("m1", needs_gpu=False, agents=agents) == "b"


def test_ties_fall_back_to_v1_warm_replica_order() -> None:
    # No data for either agent → equal (prior) churn score → v1 tiebreak: the
    # agent already holding a warm replica of the model wins.
    model = _model({})
    cold = _idle("a")
    warm = _idle("b", replicas=[make_replica_status("m1", ReplicaState.READY)])
    assert _sched(model).select_agent("m1", needs_gpu=False, agents=[cold, warm]) == "b"


def test_final_tiebreak_is_agent_id() -> None:
    model = _model({})  # both unknown → equal prior, no capability difference
    agents = [_idle("b"), _idle("a")]
    assert _sched(model).select_agent("m1", needs_gpu=False, agents=agents) == "a"


def test_pick_replica_is_least_inflight_like_v1() -> None:
    model = _model({})
    replicas = [
        make_endpoint(host="h", port=1, model_id="m1", inflight=5),
        make_endpoint(host="h", port=2, model_id="m1", inflight=1),
        make_endpoint(host="h", port=3, model_id="m2", inflight=0),
    ]
    picked = _sched(model).pick_replica("m1", replicas)
    assert picked is not None
    assert picked.port == 2  # least inflight among m1 replicas


def test_pick_replica_none_when_no_matching_model() -> None:
    model = _model({})
    replicas = [make_endpoint(host="h", port=1, model_id="other", inflight=0)]
    assert _sched(model).pick_replica("m1", replicas) is None
