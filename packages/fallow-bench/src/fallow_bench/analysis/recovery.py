"""Failure-recovery time: churn kill -> unit re-completed elsewhere.

Joins the B2 churn log (which agent died, when) to the per-unit job-status log
(which unit that agent was holding, and when it later reached ``done`` on a
*different* agent). Each such incident contributes ``t_done - t_kill``; the
reported figure is the median so one pathological retry cannot dominate.
"""

from __future__ import annotations

import pandas as pd  # type: ignore[import-untyped]

from fallow_bench.analysis.percentiles import percentile

KILL_ACTION = "agent_kill"  # B2 ChurnKind.AGENT_KILL wire value
LEASED_STATE = "leased"
DONE_STATE = "done"


def _kills_by_agent(churn: pd.DataFrame) -> dict[str, list[float]]:
    kills: dict[str, list[float]] = {}
    for row in churn.to_dict("records"):
        if row.get("action") != KILL_ACTION:
            continue
        agent, t = row.get("agent_id"), row.get("t")
        if agent is None or t is None:
            continue
        kills.setdefault(str(agent), []).append(float(t))
    for times in kills.values():
        times.sort()
    return kills


def _leases_by_unit(jobs: pd.DataFrame) -> dict[str, list[tuple[str, float]]]:
    leases: dict[str, list[tuple[str, float]]] = {}
    for row in jobs.to_dict("records"):
        if row.get("state") != LEASED_STATE:
            continue
        unit, agent, t = row.get("work_unit_id"), row.get("agent_id"), row.get("t")
        if unit is None or agent is None or t is None:
            continue
        leases.setdefault(str(unit), []).append((str(agent), float(t)))
    return leases


def _latest_kill_before(times: list[float], t_lease: float, t_done: float) -> float | None:
    candidate: float | None = None
    for tk in times:
        if t_lease <= tk < t_done:
            candidate = tk  # times are sorted, so the last match is the latest
    return candidate


def _recovery_samples(churn: pd.DataFrame, jobs: pd.DataFrame) -> list[float]:
    kills = _kills_by_agent(churn)
    leases = _leases_by_unit(jobs)
    samples: list[float] = []
    for row in jobs.to_dict("records"):
        if row.get("state") != DONE_STATE:
            continue
        unit, done_agent, t_done = row.get("work_unit_id"), row.get("agent_id"), row.get("t")
        if unit is None or t_done is None:
            continue
        best = _best_recovery(str(unit), str(done_agent), float(t_done), leases, kills)
        if best is not None:
            samples.append(best)
    return samples


def _best_recovery(
    unit: str,
    done_agent: str,
    t_done: float,
    leases: dict[str, list[tuple[str, float]]],
    kills: dict[str, list[float]],
) -> float | None:
    best: float | None = None
    for lease_agent, t_lease in leases.get(unit, []):
        if lease_agent == done_agent or lease_agent not in kills:
            continue
        tk = _latest_kill_before(kills[lease_agent], t_lease, t_done)
        if tk is None:
            continue
        recovery = t_done - tk
        if best is None or recovery < best:
            best = recovery
    return best


def failure_recovery_s(churn: pd.DataFrame, jobs: pd.DataFrame) -> float | None:
    """Median failure-recovery time in seconds across kill->recompletion incidents."""
    return percentile(_recovery_samples(churn, jobs), 50)
