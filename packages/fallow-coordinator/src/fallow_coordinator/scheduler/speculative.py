"""Bounded speculative backup-dispatch decision (scheduler module C8, ADR 056).

MapReduce's backup-task pattern, applied to batch-job tails. A job's completion
waits on its slowest unit; if the agent holding a tail unit churns (user returns,
machine sleeps) the unit only re-dispatches after a full lease-expiry cycle of
dead time. So for the *last few* units of a job, when the holder is statistically
likely to churn before it finishes, dispatch a second copy of that unit to
another idle agent. The first result wins — the queue finalizes once,
idempotently — and the loser is a no-op.

Bounded on purpose: only at the tail, only under the survival threshold, at most
one backup per unit. A trusted fleet must never double-compute a whole job, so
this never becomes blanket replication.

This module owns the *decision* only. The queue owns the crash-safe *mechanism*
(the second lease and the idempotent completion). The module DAG forbids the
queue from importing the churn model, so survival scoring lives here in the
scheduler tier and the app wires the two together. Pure: a fixed churn model plus
fixed inputs yields a fixed choice — no clock, no I/O.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from fallow_coordinator.scheduler.churn_model import ChurnModel
from fallow_protocol.messages import AgentSnapshot


@dataclass(frozen=True)
class TailUnit:
    """One in-flight tail unit a polling agent could take a backup copy of."""

    work_unit_id: str
    holder_agent_id: str
    est_duration_s: float | None


def choose_backup_unit(
    units: Sequence[TailUnit],
    holders: Mapping[str, AgentSnapshot],
    churn: ChurnModel,
    *,
    hour: int,
    survival_threshold: float,
    est_unit_duration_s: float,
) -> str | None:
    """Pick the tail unit whose holder is most likely to churn, or ``None``.

    A unit qualifies when its holder's modelled survival over the unit's expected
    runtime is *below* ``survival_threshold`` — the holder is unlikely to stay
    idle long enough to finish it, so a backup is worth its compute. Among
    qualifiers the lowest survival (most at risk) wins, ties broken on
    ``work_unit_id`` for determinism.

    A holder absent from ``holders`` (vanished from the fleet view) is skipped:
    with no live idle age to condition survival on there is no evidence to
    speculate against. Returning ``None`` means no unit is worth backing up.
    """
    best: tuple[float, str] | None = None
    for unit in units:
        holder = holders.get(unit.holder_agent_id)
        if holder is None:
            continue
        horizon = unit.est_duration_s if unit.est_duration_s is not None else est_unit_duration_s
        survival = churn.survival(holder.agent_id, hour, holder.user_idle_s, horizon)
        if survival >= survival_threshold:
            continue
        candidate = (survival, unit.work_unit_id)
        if best is None or candidate < best:
            best = candidate
    return None if best is None else best[1]
