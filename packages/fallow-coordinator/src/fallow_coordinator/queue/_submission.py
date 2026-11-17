"""Job submission helpers: unit dedup, active-job reuse, and per-unit writes.

Functions take the open connection; callers own the lock and the commit.
"""

from collections.abc import Sequence

import aiosqlite

from fallow_coordinator.queue import _sql
from fallow_protocol.messages import JobSubmit, WorkResultStatus, WorkUnitSpec, WorkUnitState


class ActiveWorkUnitConflictError(ValueError):
    """Raised when a submission would steal units from another active job."""


def dedupe_units(units: Sequence[WorkUnitSpec]) -> list[WorkUnitSpec]:
    """Drop repeated ``work_unit_id``s, keeping the first occurrence."""
    unique_units: list[WorkUnitSpec] = []
    seen_unit_ids: set[str] = set()
    for unit in units:
        if unit.work_unit_id in seen_unit_ids:
            continue
        seen_unit_ids.add(unit.work_unit_id)
        unique_units.append(unit)
    return unique_units


async def matching_active_job(
    conn: aiosqlite.Connection, job: JobSubmit, units: Sequence[WorkUnitSpec], params_json: str
) -> str | None:
    if not units:
        return None
    query = _sql.SELECT_ACTIVE_JOBS_FOR_UNITS.format(placeholders=",".join("?" for _ in units))
    cursor = await conn.execute(query, tuple(unit.work_unit_id for unit in units))
    rows = tuple(await cursor.fetchall())
    if not rows:
        return None
    matches = [
        row
        for row in rows
        if row["kind"] == job.kind.value
        and row["model_id"] == job.model_id
        and row["payload_ref"] == job.payload_ref
        and row["params_json"] == params_json
        and int(row["priority"]) == job.priority
    ]
    if len(rows) == 1 and len(matches) == 1:
        return str(matches[0]["job_id"])
    raise ActiveWorkUnitConflictError("work units already belong to another active job")


async def succeeded_unit_results(
    conn: aiosqlite.Connection, unit_ids: Sequence[str]
) -> dict[str, str]:
    if not unit_ids:
        return {}
    placeholders = ",".join("?" for _ in unit_ids)
    query = _sql.SELECT_SUCCEEDED_RESULTS.format(placeholders=placeholders)
    cursor = await conn.execute(query, tuple(unit_ids))
    rows = await cursor.fetchall()
    return {str(row["work_unit_id"]): str(row["result_ref"]) for row in rows}


async def write_submitted_unit(
    conn: aiosqlite.Connection,
    job_id: str,
    unit: WorkUnitSpec,
    deduped: dict[str, str],
    created_at: str,
) -> None:
    """Attach one unit to ``job_id``: DONE if already succeeded, else a fresh attempt."""
    is_done = unit.work_unit_id in deduped
    await conn.execute(
        _sql.SNAPSHOT_TERMINAL_OWNER,
        {"work_unit_id": unit.work_unit_id},
    )
    if not is_done:
        await conn.execute(
            _sql.DELETE_INCOMPLETE_RESULT_BINDINGS,
            {"work_unit_id": unit.work_unit_id},
        )
        await conn.execute(
            _sql.DELETE_STALE_RESULT,
            {"work_unit_id": unit.work_unit_id},
        )
        # A resubmitted unit is reset to a fresh attempt; drop any
        # backup lease left from an earlier job (ADR 056). Inert when
        # the feature is off — backup_leases is empty.
        await conn.execute(
            _sql.DELETE_BACKUP_LEASE,
            {"work_unit_id": unit.work_unit_id},
        )
    await conn.execute(
        _sql.UPSERT_WORK_UNIT,
        {
            "work_unit_id": unit.work_unit_id,
            "job_id": job_id,
            "idx": unit.idx,
            "input_ref": unit.input_ref,
            "est_duration_s": unit.est_duration_s,
            "state": (WorkUnitState.DONE.value if is_done else WorkUnitState.PENDING.value),
            "created_at": created_at,
        },
    )
    await conn.execute(
        _sql.INSERT_JOB_UNIT_MEMBERSHIP,
        {
            "job_id": job_id,
            "work_unit_id": unit.work_unit_id,
            "idx": unit.idx,
            "input_ref": unit.input_ref,
            "terminal_state": WorkUnitState.DONE.value if is_done else None,
            "result_status": WorkResultStatus.SUCCEEDED.value if is_done else None,
            "result_ref": deduped.get(unit.work_unit_id),
        },
    )
