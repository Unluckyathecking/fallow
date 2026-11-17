"""Result-payload binding and the completion checks/writes for primary leases.

Functions take the open connection; callers own the lock and the commit.
"""

import aiosqlite

from fallow_coordinator.queue import _sql
from fallow_protocol.messages import WorkResult, WorkResultStatus, WorkUnitState


async def bind_result_payload(conn: aiosqlite.Connection, params: dict[str, object]) -> bool:
    """Bind an uploaded payload (primary lane, then backup lane); True if accepted."""
    cursor = await conn.execute(_sql.BIND_RESULT_PAYLOAD, params)
    accepted = await cursor.fetchone() is not None
    if not accepted:
        cursor = await conn.execute(_sql.SELECT_MATCHING_RESULT_BINDING, params)
        accepted = await cursor.fetchone() is not None
    if not accepted:
        # Backup lane (ADR 056): authorised by a backup_leases row, bound
        # into the same table at the reserved attempt. Only a genuine
        # backup can match, so the primary path is unchanged.
        cursor = await conn.execute(_sql.BIND_RESULT_PAYLOAD_BACKUP, params)
        accepted = await cursor.fetchone() is not None
    if not accepted:
        cursor = await conn.execute(_sql.SELECT_MATCHING_BACKUP_BINDING, params)
        accepted = await cursor.fetchone() is not None
    return accepted


async def fetch_unit_for_completion(
    conn: aiosqlite.Connection, work_unit_id: str
) -> aiosqlite.Row | None:
    cursor = await conn.execute(_sql.SELECT_UNIT_FOR_COMPLETION, {"work_unit_id": work_unit_id})
    return await cursor.fetchone()


async def result_exists(conn: aiosqlite.Connection, work_unit_id: str) -> bool:
    cursor = await conn.execute(_sql.SELECT_RESULT_EXISTS, {"work_unit_id": work_unit_id})
    return await cursor.fetchone() is not None


def completion_accepted(agent_id: str, attempt: int, unit: aiosqlite.Row) -> bool:
    """Accept completion only from the current lease attempt and holder."""
    return (
        unit["state"] == WorkUnitState.LEASED.value
        and unit["lease_agent"] == agent_id
        and int(unit["attempts"]) == attempt
    )


async def result_binding_accepted(
    conn: aiosqlite.Connection, agent_id: str, attempt: int, result: WorkResult
) -> bool:
    cursor = await conn.execute(
        _sql.SELECT_RESULT_BINDING_FOR_COMPLETION,
        {
            "work_unit_id": result.work_unit_id,
            "agent_id": agent_id,
            "attempt": attempt,
            "result_ref": result.result_ref,
        },
    )
    return await cursor.fetchone() is not None


async def record_completion(
    conn: aiosqlite.Connection, job_id: str, result_params: dict[str, object], result: WorkResult
) -> None:
    """Write the result row and mark the unit and its job membership DONE."""
    await conn.execute(
        _sql.INSERT_RESULT,
        result_params,
    )
    await conn.execute(_sql.MARK_UNIT_DONE, {"work_unit_id": result.work_unit_id})
    await conn.execute(
        _sql.MARK_JOB_UNIT_TERMINAL,
        {
            "job_id": job_id,
            "work_unit_id": result.work_unit_id,
            "terminal_state": WorkUnitState.DONE.value,
            "result_status": result.status.value,
            "result_ref": (
                result.result_ref if result.status is WorkResultStatus.SUCCEEDED else None
            ),
        },
    )
