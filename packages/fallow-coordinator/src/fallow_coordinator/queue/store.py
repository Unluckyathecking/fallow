"""SQLite-backed durable job / work-unit queue with leasing (module C1).

Implements :class:`fallow_protocol.interfaces.QueueStore`. SQLite (WAL) is the
single source of truth; every mutation is a committed transaction, so the store
is crash-safe. All time is taken from an injected ``now`` callable and stored as
fixed-width ISO-8601 UTC strings.

Concurrency model: one connection guarded by an ``asyncio.Lock`` for every
mutating call. Because the event loop is single-threaded and the lock serializes
the read-then-write sequences, ``lease_next`` can never hand the same unit to two
agents (see the concurrency test).
"""

import asyncio
import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import aiosqlite

from fallow_coordinator.queue import _backup, _jobs, _requeue, _results, _sql, _submission
from fallow_coordinator.queue._backup import BackupCandidate
from fallow_coordinator.queue._constants import (
    CONNECTION_PRAGMAS,
    DEFAULT_LEASE_S,
    DEFAULT_MAX_ATTEMPTS,
    SCHEMA_FILENAME,
)
from fallow_coordinator.queue._jobs import JobDetails, JobUnitOutcome
from fallow_coordinator.queue._serialization import (
    dump_params,
    lease_expiry,
    result_row_params,
    to_iso,
)
from fallow_coordinator.queue._submission import ActiveWorkUnitConflictError
from fallow_protocol.capabilities import WorkerKind
from fallow_protocol.interfaces import QueueStore
from fallow_protocol.messages import (
    JobState,
    JobStatus,
    JobSubmit,
    UnitTransition,
    WorkResult,
    WorkResultStatus,
    WorkUnitLease,
    WorkUnitSpec,
    WorkUnitState,
)

__all__ = [
    "ActiveWorkUnitConflictError",
    "BackupCandidate",
    "JobDetails",
    "JobUnitOutcome",
    "QueueNotInitializedError",
    "SqliteQueueStore",
]

logger = logging.getLogger(__name__)


def _default_now() -> datetime:
    """Aware UTC wall-clock; the sole default time source."""
    return datetime.now(UTC)


class QueueNotInitializedError(RuntimeError):
    """Raised when the store is used before :meth:`SqliteQueueStore.init`."""


class SqliteQueueStore(QueueStore):
    """Durable, leasing job queue over a single SQLite database file."""

    def __init__(
        self,
        db_path: Path | str,
        now: Callable[[], datetime] = _default_now,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        default_lease_s: float = DEFAULT_LEASE_S,
        on_transition: Callable[[UnitTransition], None] | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if default_lease_s <= 0:
            raise ValueError("default_lease_s must be > 0")
        self._db_path = Path(db_path)
        self._now = now
        self._max_attempts = max_attempts
        self._default_lease_s = default_lease_s
        self._on_transition = on_transition
        self._lock = asyncio.Lock()
        self._conn: aiosqlite.Connection | None = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def init(self) -> None:
        """Open the connection, apply pragmas, and create the schema."""
        conn = await aiosqlite.connect(self._db_path)
        conn.row_factory = aiosqlite.Row
        for pragma in CONNECTION_PRAGMAS:
            await conn.execute(pragma)
        await conn.executescript(self._schema_sql())
        await conn.execute(_sql.BACKFILL_JOB_UNIT_MEMBERSHIPS)
        await conn.commit()
        self._conn = conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @staticmethod
    def _schema_sql() -> str:
        return (Path(__file__).with_name(SCHEMA_FILENAME)).read_text(encoding="utf-8")

    @property
    def _db(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise QueueNotInitializedError("call await store.init() before use")
        return self._conn

    def _now_utc(self) -> datetime:
        return self._now().astimezone(UTC)

    # ── submission ───────────────────────────────────────────────────────────

    async def submit_job(
        self,
        job: JobSubmit,
        units: Sequence[WorkUnitSpec],
        *,
        reuse_active: bool = False,
    ) -> str:
        units = _submission.dedupe_units(units)
        job_id = uuid4().hex
        params_json = dump_params(job.params)
        async with self._lock:
            if reuse_active:
                active_job_id = await _submission.matching_active_job(
                    self._db, job, units, params_json
                )
                if active_job_id is not None:
                    return active_job_id
            created_at = to_iso(self._now_utc())
            await self._db.execute(
                _sql.INSERT_JOB,
                {
                    "job_id": job_id,
                    "kind": job.kind.value,
                    "model_id": job.model_id,
                    "payload_ref": job.payload_ref,
                    "params_json": params_json,
                    "priority": job.priority,
                    "state": JobState.PENDING.value,
                    "created_at": created_at,
                },
            )
            deduped = await _submission.succeeded_unit_results(
                self._db, [u.work_unit_id for u in units]
            )
            for unit in units:
                await _submission.write_submitted_unit(self._db, job_id, unit, deduped, created_at)
            await _jobs.recompute_job_state(self._db, job_id)
            await self._db.commit()
        return job_id

    # ── leasing ──────────────────────────────────────────────────────────────

    async def lease_next(self, agent_id: str, model_ids: Sequence[str]) -> WorkUnitLease | None:
        if not model_ids:
            return None
        async with self._lock:
            candidate = await self._select_candidate(model_ids)
            if candidate is None:
                return None
            now = self._now_utc()
            expiry = lease_expiry(now, candidate["est_duration_s"], self._default_lease_s)
            cursor = await self._db.execute(
                _sql.CLAIM_UNIT,
                {
                    "agent_id": agent_id,
                    "lease_expires": to_iso(expiry),
                    "work_unit_id": candidate["work_unit_id"],
                },
            )
            claimed = await cursor.fetchone()
            if claimed is None:  # lost the race (defensive; lock prevents it)
                return None
            await _jobs.recompute_job_state(self._db, str(candidate["job_id"]))
            await self._db.commit()
            transition = UnitTransition(
                work_unit_id=str(candidate["work_unit_id"]),
                job_id=str(candidate["job_id"]),
                agent_id=agent_id,
                attempt=int(claimed["attempts"]),
                state=WorkUnitState.LEASED,
                at=now,
            )
            lease = WorkUnitLease(
                work_unit_id=str(candidate["work_unit_id"]),
                job_id=str(candidate["job_id"]),
                kind=WorkerKind(str(candidate["kind"])),
                model_id=str(candidate["model_id"]),
                input_url=str(candidate["input_ref"]),
                lease_expires=expiry,
                attempt=int(claimed["attempts"]),
                est_duration_s=candidate["est_duration_s"],
            )
        self._emit_transition(transition)
        return lease

    def _emit_transition(self, transition: UnitTransition) -> None:
        if self._on_transition is None:
            return
        try:
            self._on_transition(transition)
        except Exception:
            logger.exception("work-unit transition observer failed")

    async def _select_candidate(self, model_ids: Sequence[str]) -> aiosqlite.Row | None:
        placeholders = ",".join("?" for _ in model_ids)
        query = _sql.SELECT_LEASE_CANDIDATE.format(placeholders=placeholders)
        cursor = await self._db.execute(query, tuple(model_ids))
        return await cursor.fetchone()

    async def extend_leases(self, agent_id: str, unit_ids: Sequence[str]) -> None:
        if not unit_ids:
            return
        async with self._lock:
            expiry = lease_expiry(self._now_utc(), None, self._default_lease_s)
            placeholders = ",".join("?" for _ in unit_ids)
            query = _sql.EXTEND_LEASES.format(placeholders=placeholders)
            await self._db.execute(query, (to_iso(expiry), agent_id, *unit_ids))
            await self._db.commit()

    # ── speculative backup dispatch (ADR 056) ─────────────────────────────────

    @property
    def _backup_attempt(self) -> int:
        """Reserved lease attempt for a backup's payload binding.

        One past the retry budget, so a backup's ``(work_unit_id, attempt)``
        binding can never collide with a primary attempt's for the same unit
        (primary attempts run ``1..max_attempts`` before a unit goes dead).
        """
        return self._max_attempts + 1

    async def backup_candidates(
        self, agent_id: str, model_ids: Sequence[str], tail_max_remaining: int
    ) -> list[BackupCandidate]:
        """In-flight tail units ``agent_id`` could take a backup copy of.

        A candidate is a unit currently leased to *another* agent, for a model in
        ``model_ids``, with no backup yet, not finalized, whose job has at most
        ``tail_max_remaining`` unfinished units. The survival decision — whether
        the holder is actually likely to churn — is the scheduler's, not the
        queue's, so this only surfaces the facts.
        """
        if not model_ids:
            return []
        async with self._lock:
            return await _backup.select_backup_candidates(
                self._db, agent_id, model_ids, tail_max_remaining
            )

    async def lease_backup(self, agent_id: str, work_unit_id: str) -> WorkUnitLease | None:
        """Hand ``agent_id`` a bounded second lease on an in-flight unit.

        Returns ``None`` unless the unit is currently leased to a *different*
        agent and has no backup yet (at most one backup per unit, enforced by the
        ``backup_leases`` primary key). The primary lease is untouched: whoever
        completes first finalizes the unit, the other is an idempotent no-op.
        """
        async with self._lock:
            lease = await _backup.insert_backup_lease(
                self._db,
                agent_id,
                work_unit_id,
                now_utc=self._now_utc,
                default_lease_s=self._default_lease_s,
                attempt=self._backup_attempt,
            )
            if lease is None:
                return None
            await self._db.commit()
        return lease

    # ── completion ───────────────────────────────────────────────────────────

    async def result_upload_attempt(self, agent_id: str, work_unit_id: str) -> int | None:
        """Return the active attempt when ``agent_id`` currently holds the lease.

        A backup holder (ADR 056) gets its reserved attempt back instead, so the
        shared upload route authorises it too. Checked only when the primary
        lookup misses, so a deployment with the feature off is unaffected.
        """
        async with self._lock:
            cursor = await self._db.execute(
                _sql.SELECT_RESULT_UPLOAD_ATTEMPT,
                {"agent_id": agent_id, "work_unit_id": work_unit_id},
            )
            row = await cursor.fetchone()
            if row is not None:
                return int(row["attempts"])
            cursor = await self._db.execute(
                _sql.SELECT_BACKUP_UPLOAD_ATTEMPT,
                {"agent_id": agent_id, "work_unit_id": work_unit_id},
            )
            backup = await cursor.fetchone()
            return None if backup is None else int(backup["attempt"])

    async def bind_result_payload(
        self,
        agent_id: str,
        work_unit_id: str,
        attempt: int,
        digest: str,
        result_ref: str,
    ) -> bool:
        """Bind an uploaded payload only while its lease snapshot is current."""
        params: dict[str, object] = {
            "agent_id": agent_id,
            "work_unit_id": work_unit_id,
            "attempt": attempt,
            "digest": digest,
            "result_ref": result_ref,
            "accepted_at": to_iso(self._now_utc()),
        }
        async with self._lock:
            accepted = await _results.bind_result_payload(self._db, params)
            await self._db.commit()
            return accepted

    async def complete_unit(self, agent_id: str, attempt: int, result: WorkResult) -> bool:
        async with self._lock:
            unit = await _results.fetch_unit_for_completion(self._db, result.work_unit_id)
            if unit is None:
                return False
            completed_at = self._now_utc()
            result_params = result_row_params(result, agent_id, to_iso(completed_at))
            if await _results.result_exists(self._db, result.work_unit_id):
                cursor = await self._db.execute(
                    _sql.SELECT_MATCHING_COMPLETION,
                    {**result_params, "attempt": attempt},
                )
                if await cursor.fetchone() is not None:
                    return True
                # A backup whose unit the other holder already finalized: a clean,
                # idempotent no-op (ADR 056). First completion won; nothing to apply.
                return await _backup.backup_holder_exists(
                    self._db, result.work_unit_id, agent_id, attempt
                )
            is_primary = _results.completion_accepted(agent_id, attempt, unit)
            is_backup = not is_primary and await _backup.backup_role(
                self._db, result.work_unit_id, agent_id, attempt, unit
            )
            if not (is_primary or is_backup):
                return False
            if result.status is WorkResultStatus.SUCCEEDED:
                bound = (
                    await _results.result_binding_accepted(self._db, agent_id, attempt, result)
                    if is_primary
                    else await _backup.backup_binding_accepted(self._db, agent_id, attempt, result)
                )
                if not bound:
                    return False
            await _results.record_completion(self._db, str(unit["job_id"]), result_params, result)
            await _jobs.recompute_job_state(self._db, str(unit["job_id"]))
            await self._db.commit()
            transition = UnitTransition(
                work_unit_id=result.work_unit_id,
                job_id=str(unit["job_id"]),
                agent_id=agent_id,
                attempt=int(unit["attempts"]),
                state=WorkUnitState.DONE,
                at=completed_at,
            )
        self._emit_transition(transition)
        return True

    # ── requeue ──────────────────────────────────────────────────────────────

    async def completed_result_ref(self, work_unit_id: str) -> str | None:
        """Return an accepted payload reference only after successful completion."""
        async with self._lock:
            cursor = await self._db.execute(
                _sql.SELECT_COMPLETED_RESULT_REF, {"work_unit_id": work_unit_id}
            )
            row = await cursor.fetchone()
            return None if row is None else str(row["result_ref"])

    async def requeue_expired(self) -> int:
        async with self._lock:
            at = self._now_utc()
            transitions = await _requeue.requeue(
                self._db, _sql.SELECTOR_EXPIRED, {"now": to_iso(at)}, self._max_attempts, at=at
            )
            await self._db.commit()
        for transition in transitions:
            self._emit_transition(transition)
        return len(transitions)

    async def requeue_agent(self, agent_id: str) -> int:
        async with self._lock:
            transitions = await _requeue.requeue(
                self._db,
                _sql.SELECTOR_BY_AGENT,
                {"agent_id": agent_id},
                self._max_attempts,
                at=self._now_utc(),
            )
            await self._db.commit()
        for transition in transitions:
            self._emit_transition(transition)
        return len(transitions)

    # ── status ───────────────────────────────────────────────────────────────

    async def job_status(self, job_id: str) -> JobStatus | None:
        cursor = await self._db.execute(_sql.SELECT_JOB_STATE, {"job_id": job_id})
        job_row = await cursor.fetchone()
        if job_row is None:
            return None
        counts = await _jobs.unit_counts(self._db, job_id)
        return JobStatus(
            job_id=job_id,
            state=JobState(str(job_row["state"])),
            total_units=counts.total,
            done_units=counts.done,
            dead_units=counts.dead,
        )

    async def job_details(self, job_id: str) -> JobDetails | None:
        return await _jobs.job_details(self._db, job_id)

    async def job_finalization(self, job_id: str) -> int | None:
        cursor = await self._db.execute(_sql.SELECT_JOB_FINALIZATION, {"job_id": job_id})
        row = await cursor.fetchone()
        return None if row is None else int(row["indexed_items"])

    async def mark_job_finalized(self, job_id: str, indexed_items: int) -> int:
        if indexed_items < 0:
            raise ValueError("indexed_items must be >= 0")
        async with self._lock:
            await self._db.execute(
                _sql.INSERT_JOB_FINALIZATION,
                {"job_id": job_id, "indexed_items": indexed_items},
            )
            cursor = await self._db.execute(
                _sql.SELECT_JOB_FINALIZATION,
                {"job_id": job_id},
            )
            row = await cursor.fetchone()
            assert row is not None
            await self._db.commit()
            return int(row["indexed_items"])
