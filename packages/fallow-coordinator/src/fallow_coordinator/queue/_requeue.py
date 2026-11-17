"""Lease-expiry and agent-eviction requeue: leased units go back to pending, or
to dead once their retry budget is spent.

Functions take the open connection; callers own the lock, the commit, and
emitting the returned transitions.
"""

from datetime import datetime

import aiosqlite

from fallow_coordinator.queue import _sql
from fallow_coordinator.queue._jobs import recompute_job_state
from fallow_protocol.messages import UnitTransition, WorkUnitState


async def requeue(
    conn: aiosqlite.Connection,
    selector: str,
    selector_params: dict[str, object],
    max_attempts: int,
    *,
    at: datetime,
) -> list[UnitTransition]:
    params = {**selector_params, "max_attempts": max_attempts}
    pending = await _run_requeue_branch(
        conn, _sql.REQUEUE_TO_PENDING.format(selector=selector), params, WorkUnitState.PENDING, at
    )
    dead = await _run_requeue_branch(
        conn, _sql.REQUEUE_TO_DEAD.format(selector=selector), params, WorkUnitState.DEAD, at
    )
    transitions = sorted((*pending, *dead), key=lambda transition: transition.work_unit_id)
    for transition in dead:
        await conn.execute(
            _sql.MARK_JOB_UNIT_TERMINAL,
            {
                "job_id": transition.job_id,
                "work_unit_id": transition.work_unit_id,
                "terminal_state": WorkUnitState.DEAD.value,
                "result_status": None,
                "result_ref": None,
            },
        )
    for job_id in {transition.job_id for transition in transitions}:
        await recompute_job_state(conn, job_id)
    return transitions


async def _run_requeue_branch(
    conn: aiosqlite.Connection,
    query: str,
    params: dict[str, object],
    state: WorkUnitState,
    at: datetime,
) -> list[UnitTransition]:
    cursor = await conn.execute(query, params)
    rows = await cursor.fetchall()
    return [
        UnitTransition(
            work_unit_id=str(row["work_unit_id"]),
            job_id=str(row["job_id"]),
            agent_id=str(row["lease_agent"]),
            attempt=int(row["attempts"]),
            state=state,
            at=at,
        )
        for row in rows
    ]
