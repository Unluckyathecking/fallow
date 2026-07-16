"""Tests for reliability folded into ChurnAwareScheduler ranking (C7).

These assert the intended hierarchy: idle-survival is the primary signal and
task-success reliability is a bounded secondary weight. A survival gap of at
least ``reliability_weight`` is never overturned by reliability; only genuine
near-ties are shaped by it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from scheduler_helpers import make_agent

from fallow_coordinator.scheduler.churn_model import ChurnModel
from fallow_coordinator.scheduler.reliability import ReliabilityModel
from fallow_coordinator.scheduler.v2 import DEFAULT_RELIABILITY_WEIGHT, ChurnAwareScheduler
from fallow_protocol.messages import AgentSnapshot, AgentState

FIXED_HOUR = 12

# Survival over the default 60s horizon is (samples >= 60) / (all samples); these
# pools are engineered to yield exact survival fractions at idle age 0. ChurnModel
# expects each pool sorted ascending (it counts via binary search).
_SURVIVAL_1_00 = (100.0,) * 5  # 5/5
_SURVIVAL_0_90 = (30.0,) + (100.0,) * 9  # 9/10
_SURVIVAL_0_85 = (30.0,) * 3 + (100.0,) * 17  # 17/20
_SURVIVAL_0_80 = (30.0,) * 2 + (100.0,) * 8  # 8/10


def _churn(pool: Mapping[str, tuple[float, ...]]) -> ChurnModel:
    return ChurnModel(by_bucket={}, by_agent=dict(pool), optimistic_prior=0.9)


def _sched(
    churn: ChurnModel,
    reliability: ReliabilityModel | None,
    *,
    weight: float = DEFAULT_RELIABILITY_WEIGHT,
) -> ChurnAwareScheduler:
    return ChurnAwareScheduler(
        churn,
        60.0,
        hour_fn=lambda: FIXED_HOUR,
        reliability=reliability,
        reliability_weight=weight,
    )


def _agents(*agent_ids: str) -> Sequence[AgentSnapshot]:
    return [make_agent(agent_id) for agent_id in agent_ids]


def test_reliability_breaks_a_survival_tie() -> None:
    # Both agents have identical (perfect) survival, so churn alone ties them and
    # the pick would fall to agent_id ("a" < "z"). Reliability flips it: "z"
    # finishes work, "a" drops it, so "z" wins despite the id order.
    churn = _churn({"a": _SURVIVAL_1_00, "z": _SURVIVAL_1_00})
    reliability = ReliabilityModel(by_agent={"a": (0, 5), "z": (5, 0)})
    assert _sched(churn, reliability).select_agent("m1", False, _agents("a", "z")) == "z"


def test_without_reliability_the_same_tie_falls_to_agent_id() -> None:
    # Same setup, no reliability model → pure survival ranking, so the tie falls to
    # agent_id and "a" wins. Confirms the fold-in is what moved the winner above.
    churn = _churn({"a": _SURVIVAL_1_00, "z": _SURVIVAL_1_00})
    assert _sched(churn, None).select_agent("m1", False, _agents("a", "z")) == "a"


def test_survival_dominates_beyond_the_weight_band() -> None:
    # "hi" leads survival by 0.2 (1.00 vs 0.80), more than the 0.1 weight. Even
    # with the worst possible reliability (0.0) against a perfect (1.0) rival, its
    # survival lead is decisive: 1.00 + 0 > 0.80 + 0.1.
    churn = _churn({"hi": _SURVIVAL_1_00, "lo": _SURVIVAL_0_80})
    reliability = ReliabilityModel(by_agent={"hi": (0, 5), "lo": (5, 0)})
    assert _sched(churn, reliability).select_agent("m1", False, _agents("hi", "lo")) == "hi"


def test_reliability_shapes_a_near_tie_within_the_band() -> None:
    # Survival gap is only 0.05 (0.90 vs 0.85), inside the 0.1 band. The trailing
    # agent's perfect reliability overturns it: 0.85 + 0.1 > 0.90 + 0.
    churn = _churn({"lead": _SURVIVAL_0_90, "reliable": _SURVIVAL_0_85})
    reliability = ReliabilityModel(by_agent={"lead": (0, 5), "reliable": (5, 0)})
    assert _sched(churn, reliability).select_agent("m1", False, _agents("lead", "reliable")) == (
        "reliable"
    )


def test_unseen_agents_share_the_prior_so_reliability_is_neutral() -> None:
    # Neither agent appears in the reliability model → both get the same prior, so
    # reliability contributes an equal constant and survival decides as before.
    churn = _churn({"a": _SURVIVAL_0_90, "b": _SURVIVAL_0_80})
    reliability = ReliabilityModel(by_agent={"someone-else": (5, 0)})
    assert _sched(churn, reliability).select_agent("m1", False, _agents("a", "b")) == "a"


def test_eligibility_gate_still_precedes_scoring() -> None:
    # A perfectly reliable, high-survival agent that is ACTIVE (ineligible) must
    # not be selected over a weak but eligible one. Reliability cannot rescue it.
    churn = _churn({"busy": _SURVIVAL_1_00, "free": _SURVIVAL_0_80})
    reliability = ReliabilityModel(by_agent={"busy": (5, 0), "free": (0, 5)})
    agents = [make_agent("busy", state=AgentState.ACTIVE), make_agent("free")]
    assert _sched(churn, reliability).select_agent("m1", False, agents) == "free"
