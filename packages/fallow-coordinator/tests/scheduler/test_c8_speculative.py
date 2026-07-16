"""The speculative backup decision (module C8, ADR 056).

Pure survival math: given tail units and their holders, pick the one whose holder
is most likely to churn — or nobody. No clock, no I/O, so every case is a fixed
input to a fixed output.
"""

from __future__ import annotations

from scheduler_helpers import make_agent

from fallow_coordinator.scheduler import TailUnit, choose_backup_unit
from fallow_coordinator.scheduler.churn_model import ChurnModel
from fallow_protocol.messages import AgentSnapshot

HOUR = 12
HORIZON = 60.0

# A holder idle only ~a minute in a bucket of short sessions: unlikely to last
# another HORIZON seconds, so survival is low. Sessions are (agent, hour) → lengths.
_SHORT_SESSIONS: dict[tuple[str, int], tuple[float, ...]] = {
    ("flaky", HOUR): (30.0, 40.0, 50.0, 55.0, 60.0),
}
# A holder in a bucket of long sessions: very likely to outlast the horizon.
_LONG_SESSIONS: dict[tuple[str, int], tuple[float, ...]] = {
    ("steady", HOUR): (600.0, 700.0, 800.0, 900.0, 1000.0),
}


def _model(buckets: dict[tuple[str, int], tuple[float, ...]], prior: float = 0.9) -> ChurnModel:
    pool = {agent: lengths for (agent, _hour), lengths in buckets.items()}
    return ChurnModel(by_bucket=buckets, by_agent=pool, optimistic_prior=prior)


def _choose(
    units: list[TailUnit],
    holders: dict[str, AgentSnapshot],
    churn: ChurnModel,
    threshold: float,
) -> str | None:
    return choose_backup_unit(
        units,
        holders,
        churn,
        hour=HOUR,
        survival_threshold=threshold,
        est_unit_duration_s=HORIZON,
    )


def test_backs_up_a_tail_unit_whose_holder_is_likely_to_churn() -> None:
    churn = _model(_SHORT_SESSIONS)
    holder = make_agent("flaky")  # user_idle_s defaults to 0.0
    units = [TailUnit("u-tail", "flaky", est_duration_s=HORIZON)]

    # survival("flaky", idle 0, horizon 60) = |{s >= 60}| / |{s >= 0}| = 1/5 = 0.2
    assert _choose(units, {"flaky": holder}, churn, threshold=0.5) == "u-tail"


def test_high_survival_holder_is_never_backed_up() -> None:
    churn = _model(_LONG_SESSIONS)
    holder = make_agent("steady")
    units = [TailUnit("u-tail", "steady", est_duration_s=HORIZON)]

    # Every session outlasts the horizon → survival 1.0, well above the threshold.
    assert _choose(units, {"steady": holder}, churn, threshold=0.5) is None


def test_no_candidates_means_no_speculation() -> None:
    # An empty candidate set is the not-a-tail case: nothing to back up.
    assert _choose([], {}, _model(_SHORT_SESSIONS), threshold=0.99) is None


def test_holder_missing_from_the_fleet_view_is_skipped() -> None:
    churn = _model(_SHORT_SESSIONS)
    units = [TailUnit("u-tail", "flaky", est_duration_s=HORIZON)]
    # No snapshot for the holder → no idle age to condition on → skip.
    assert _choose(units, {}, churn, threshold=0.5) is None


def test_picks_the_most_at_risk_among_several_tail_units() -> None:
    # "flaky" survives 0.2; "steady" survives 1.0. Both are candidates only under a
    # high threshold, but the most-at-risk (lowest survival) wins.
    churn = _model({**_SHORT_SESSIONS, **_LONG_SESSIONS})
    holders = {"flaky": make_agent("flaky"), "steady": make_agent("steady")}
    units = [
        TailUnit("u-steady", "steady", est_duration_s=HORIZON),
        TailUnit("u-flaky", "flaky", est_duration_s=HORIZON),
    ]
    assert _choose(units, holders, churn, threshold=1.01) == "u-flaky"


def test_ties_break_deterministically_on_work_unit_id() -> None:
    # Two holders with identical (unseen) survival = prior 0.9; lowest id wins.
    churn = _model({}, prior=0.5)
    holders = {"a1": make_agent("a1"), "a2": make_agent("a2")}
    units = [
        TailUnit("u-second", "a2", est_duration_s=HORIZON),
        TailUnit("u-first", "a1", est_duration_s=HORIZON),
    ]
    # prior 0.5 is below a 0.6 threshold, so both qualify; tie on survival → id order.
    assert _choose(units, holders, churn, threshold=0.6) == "u-first"
