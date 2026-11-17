"""Job-level reads and the job-state recompute shared by every mutator.

Functions take the open connection; callers own the lock and the commit.
"""

import json
from dataclasses import dataclass

import aiosqlite

from fallow_coordinator.queue import _sql
from fallow_coordinator.queue._jobstate import UnitCounts, next_job_state
from fallow_protocol.messages import WorkResultStatus, WorkUnitState


@dataclass(frozen=True)
class JobUnitOutcome:
    work_unit_id: str
    idx: int
    input_ref: str
    state: WorkUnitState
    result_status: WorkResultStatus | None
    result_ref: str | None


@dataclass(frozen=True)
class JobDetails:
    model_id: str
    params: dict[str, str]
    units: tuple[JobUnitOutcome, ...]


async def job_details(conn: aiosqlite.Connection, job_id: str) -> JobDetails | None:
    job_cursor = await conn.execute(_sql.SELECT_JOB_DETAILS, {"job_id": job_id})
    job = await job_cursor.fetchone()
    if job is None:
        return None
    unit_cursor = await conn.execute(_sql.SELECT_JOB_UNIT_OUTCOMES, {"job_id": job_id})
    units = await unit_cursor.fetchall()
    raw_params = json.loads(str(job["params_json"]))
    if not isinstance(raw_params, dict):  # pragma: no cover - writer always stores object
        raise RuntimeError("stored job params are not an object")
    return JobDetails(
        model_id=str(job["model_id"]),
        params={str(key): str(value) for key, value in raw_params.items()},
        units=tuple(
            JobUnitOutcome(
                work_unit_id=str(row["work_unit_id"]),
                idx=int(row["idx"]),
                input_ref=str(row["input_ref"]),
                state=WorkUnitState(str(row["state"])),
                result_status=(
                    None
                    if row["result_status"] is None
                    else WorkResultStatus(str(row["result_status"]))
                ),
                result_ref=None if row["result_ref"] is None else str(row["result_ref"]),
            )
            for row in units
        ),
    )


async def unit_counts(conn: aiosqlite.Connection, job_id: str) -> UnitCounts:
    cursor = await conn.execute(_sql.COUNT_JOB_UNITS, {"job_id": job_id})
    row = await cursor.fetchone()
    assert row is not None  # aggregate always returns one row
    return UnitCounts(
        total=int(row["total"]),
        pending=int(row["pending"]),
        leased=int(row["leased"]),
        done=int(row["done"]),
        dead=int(row["dead"]),
    )


async def recompute_job_state(conn: aiosqlite.Connection, job_id: str) -> None:
    counts = await unit_counts(conn, job_id)
    await conn.execute(_sql.SET_JOB_STATE, {"state": next_job_state(counts), "job_id": job_id})
