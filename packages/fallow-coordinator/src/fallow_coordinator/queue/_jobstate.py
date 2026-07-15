"""Pure job-lifecycle decision logic derived from unit-state counts."""

from dataclasses import dataclass

from fallow_coordinator.queue._sql import JOB_DONE, JOB_PENDING, JOB_RUNNING


@dataclass(frozen=True)
class UnitCounts:
    """Per-job tally of work-unit states."""

    total: int
    pending: int
    leased: int
    done: int
    dead: int


def next_job_state(counts: UnitCounts) -> str:
    """Terminal once no unit can still run; RUNNING once any unit has moved off
    PENDING; PENDING while every unit is still waiting.

    A job with no units is trivially DONE.
    """
    if counts.total == 0 or (counts.pending == 0 and counts.leased == 0):
        return JOB_DONE
    if counts.done + counts.dead + counts.leased > 0:
        return JOB_RUNNING
    return JOB_PENDING
