"""Speculative backup dispatch (ADR 056): the second-lease lane.

The backup lane never mutates work_units or the primary's bindings; see the
banner in ``_sql.py``. Functions take the open connection; callers own the
lock and the commit.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

import aiosqlite

from fallow_coordinator.queue import _sql
from fallow_coordinator.queue._serialization import lease_expiry, to_iso
from fallow_protocol.capabilities import WorkerKind
from fallow_protocol.messages import WorkResult, WorkUnitLease, WorkUnitState


@dataclass(frozen=True)
class BackupCandidate:
    """An in-flight tail unit a polling agent could take a backup copy of.

    Carries only the facts the scheduler's survival decision needs; the decision
    itself (which needs the churn model, a higher layer) lives outside the queue.
    """

    work_unit_id: str
    job_id: str
    model_id: str
    holder_agent_id: str
    est_duration_s: float | None


async def select_backup_candidates(
    conn: aiosqlite.Connection, agent_id: str, model_ids: Sequence[str], tail_max_remaining: int
) -> list[BackupCandidate]:
    placeholders = ",".join("?" for _ in model_ids)
    query = _sql.SELECT_BACKUP_CANDIDATES.format(placeholders=placeholders)
    cursor = await conn.execute(query, (*model_ids, agent_id, tail_max_remaining))
    rows = await cursor.fetchall()
    return [
        BackupCandidate(
            work_unit_id=str(row["work_unit_id"]),
            job_id=str(row["job_id"]),
            model_id=str(row["model_id"]),
            holder_agent_id=str(row["holder_agent_id"]),
            est_duration_s=(
                None if row["est_duration_s"] is None else float(row["est_duration_s"])
            ),
        )
        for row in rows
    ]


async def insert_backup_lease(
    conn: aiosqlite.Connection,
    agent_id: str,
    work_unit_id: str,
    *,
    now_utc: Callable[[], datetime],
    default_lease_s: float,
    attempt: int,
) -> WorkUnitLease | None:
    """Record a backup lease and return it, or ``None`` when the unit is not a
    backup target or already has a backup. Does not commit."""
    cursor = await conn.execute(
        _sql.SELECT_BACKUP_LEASE_TARGET,
        {"work_unit_id": work_unit_id, "agent_id": agent_id},
    )
    target = await cursor.fetchone()
    if target is None:
        return None
    now = now_utc()
    expiry = lease_expiry(now, target["est_duration_s"], default_lease_s)
    cursor = await conn.execute(
        _sql.INSERT_BACKUP_LEASE,
        {
            "work_unit_id": work_unit_id,
            "job_id": str(target["job_id"]),
            "agent_id": agent_id,
            "attempt": attempt,
            "lease_expires": to_iso(expiry),
            "created_at": to_iso(now),
        },
    )
    inserted = await cursor.fetchone()
    if inserted is None:
        return None
    return WorkUnitLease(
        work_unit_id=work_unit_id,
        job_id=str(target["job_id"]),
        kind=WorkerKind(str(target["kind"])),
        model_id=str(target["model_id"]),
        input_url=str(target["input_ref"]),
        lease_expires=expiry,
        attempt=attempt,
        est_duration_s=target["est_duration_s"],
    )


async def backup_holder_exists(
    conn: aiosqlite.Connection, work_unit_id: str, agent_id: str, attempt: int
) -> bool:
    """True when ``agent_id`` holds the backup lease for this unit and attempt."""
    cursor = await conn.execute(
        _sql.SELECT_BACKUP_LEASE_HOLDER,
        {"work_unit_id": work_unit_id, "agent_id": agent_id, "attempt": attempt},
    )
    return await cursor.fetchone() is not None


async def backup_role(
    conn: aiosqlite.Connection,
    work_unit_id: str,
    agent_id: str,
    attempt: int,
    unit: aiosqlite.Row,
) -> bool:
    """Accept a backup completion only while the unit can still be finalized.

    A backup may finalize a unit that is leased (the common race) or has been
    requeued to pending (its primary expired — the backup salvages it before
    a re-lease). A dead unit is left dead; a done unit is handled earlier by
    the result-exists no-op.
    """
    if unit["state"] not in (WorkUnitState.LEASED.value, WorkUnitState.PENDING.value):
        return False
    return await backup_holder_exists(conn, work_unit_id, agent_id, attempt)


async def backup_binding_accepted(
    conn: aiosqlite.Connection, agent_id: str, attempt: int, result: WorkResult
) -> bool:
    cursor = await conn.execute(
        _sql.SELECT_BACKUP_BINDING_FOR_COMPLETION,
        {
            "work_unit_id": result.work_unit_id,
            "agent_id": agent_id,
            "attempt": attempt,
            "result_ref": result.result_ref,
        },
    )
    return await cursor.fetchone() is not None
