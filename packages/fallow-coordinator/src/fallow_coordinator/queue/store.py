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
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import aiosqlite

from fallow_coordinator.queue import _sql
from fallow_coordinator.queue._constants import (
    CONNECTION_PRAGMAS,
    DEFAULT_LEASE_S,
    DEFAULT_MAX_ATTEMPTS,
    SCHEMA_FILENAME,
)
from fallow_coordinator.queue._jobstate import UnitCounts, next_job_state
from fallow_coordinator.queue._serialization import (
    dump_params,
    lease_expiry,
    result_row_params,
    to_iso,
)
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

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JobUnitOutcome:
    work_unit_id: str
    idx: int
    input_ref: str
    state: WorkUnitState
    result_ref: str | None


@dataclass(frozen=True)
class JobDetails:
    model_id: str
    params: dict[str, str]
    units: tuple[JobUnitOutcome, ...]


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

    async def submit_job(self, job: JobSubmit, units: Sequence[WorkUnitSpec]) -> str:
        job_id = uuid4().hex
        async with self._lock:
            created_at = to_iso(self._now_utc())
            await self._db.execute(
                _sql.INSERT_JOB,
                {
                    "job_id": job_id,
                    "kind": job.kind.value,
                    "model_id": job.model_id,
                    "payload_ref": job.payload_ref,
                    "params_json": dump_params(job.params),
                    "priority": job.priority,
                    "state": JobState.PENDING.value,
                    "created_at": created_at,
                },
            )
            deduped = await self._succeeded_unit_ids([u.work_unit_id for u in units])
            for unit in units:
                is_done = unit.work_unit_id in deduped
                if not is_done:
                    await self._db.execute(
                        _sql.DELETE_INCOMPLETE_RESULT_BINDINGS,
                        {"work_unit_id": unit.work_unit_id},
                    )
                await self._db.execute(
                    _sql.UPSERT_WORK_UNIT,
                    {
                        "work_unit_id": unit.work_unit_id,
                        "job_id": job_id,
                        "idx": unit.idx,
                        "input_ref": unit.input_ref,
                        "est_duration_s": unit.est_duration_s,
                        "state": (
                            WorkUnitState.DONE.value if is_done else WorkUnitState.PENDING.value
                        ),
                        "created_at": created_at,
                    },
                )
            await self._recompute_job_state(job_id)
            await self._db.commit()
        return job_id

    async def _succeeded_unit_ids(self, unit_ids: Sequence[str]) -> set[str]:
        if not unit_ids:
            return set()
        placeholders = ",".join("?" for _ in unit_ids)
        query = _sql.SELECT_SUCCEEDED_RESULTS.format(placeholders=placeholders)
        cursor = await self._db.execute(query, tuple(unit_ids))
        rows = await cursor.fetchall()
        return {str(row["work_unit_id"]) for row in rows}

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
            await self._recompute_job_state(str(candidate["job_id"]))
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

    # ── completion ───────────────────────────────────────────────────────────

    async def result_upload_attempt(self, agent_id: str, work_unit_id: str) -> int | None:
        """Return the active attempt when ``agent_id`` currently holds the lease."""
        async with self._lock:
            cursor = await self._db.execute(
                _sql.SELECT_RESULT_UPLOAD_ATTEMPT,
                {"agent_id": agent_id, "work_unit_id": work_unit_id},
            )
            row = await cursor.fetchone()
            return None if row is None else int(row["attempts"])

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
            cursor = await self._db.execute(_sql.BIND_RESULT_PAYLOAD, params)
            inserted = await cursor.fetchone()
            if inserted is None:
                cursor = await self._db.execute(_sql.SELECT_MATCHING_RESULT_BINDING, params)
                accepted = await cursor.fetchone() is not None
            else:
                accepted = True
            await self._db.commit()
            return accepted

    async def complete_unit(self, agent_id: str, attempt: int, result: WorkResult) -> bool:
        async with self._lock:
            unit = await self._fetch_unit_for_completion(result.work_unit_id)
            if unit is None:
                return False
            completed_at = self._now_utc()
            result_params = result_row_params(result, agent_id, to_iso(completed_at))
            if await self._result_exists(result.work_unit_id):
                cursor = await self._db.execute(
                    _sql.SELECT_MATCHING_COMPLETION,
                    {**result_params, "attempt": attempt},
                )
                return await cursor.fetchone() is not None
            if not self._completion_accepted(agent_id, attempt, unit):
                return False
            if (
                result.status is WorkResultStatus.SUCCEEDED
                and not await self._result_binding_accepted(agent_id, attempt, result)
            ):
                return False
            await self._db.execute(
                _sql.INSERT_RESULT,
                result_params,
            )
            await self._db.execute(_sql.MARK_UNIT_DONE, {"work_unit_id": result.work_unit_id})
            await self._recompute_job_state(str(unit["job_id"]))
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

    async def _fetch_unit_for_completion(self, work_unit_id: str) -> aiosqlite.Row | None:
        cursor = await self._db.execute(
            _sql.SELECT_UNIT_FOR_COMPLETION, {"work_unit_id": work_unit_id}
        )
        return await cursor.fetchone()

    async def _result_exists(self, work_unit_id: str) -> bool:
        cursor = await self._db.execute(_sql.SELECT_RESULT_EXISTS, {"work_unit_id": work_unit_id})
        return await cursor.fetchone() is not None

    def _completion_accepted(self, agent_id: str, attempt: int, unit: aiosqlite.Row) -> bool:
        """Accept completion only from the current lease attempt and holder."""
        return (
            unit["state"] == WorkUnitState.LEASED.value
            and unit["lease_agent"] == agent_id
            and int(unit["attempts"]) == attempt
        )

    async def _result_binding_accepted(
        self, agent_id: str, attempt: int, result: WorkResult
    ) -> bool:
        cursor = await self._db.execute(
            _sql.SELECT_RESULT_BINDING_FOR_COMPLETION,
            {
                "work_unit_id": result.work_unit_id,
                "agent_id": agent_id,
                "attempt": attempt,
                "result_ref": result.result_ref,
            },
        )
        return await cursor.fetchone() is not None

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
            transitions = await self._requeue(_sql.SELECTOR_EXPIRED, {"now": to_iso(at)}, at=at)
            await self._db.commit()
        for transition in transitions:
            self._emit_transition(transition)
        return len(transitions)

    async def requeue_agent(self, agent_id: str) -> int:
        async with self._lock:
            transitions = await self._requeue(
                _sql.SELECTOR_BY_AGENT, {"agent_id": agent_id}, at=self._now_utc()
            )
            await self._db.commit()
        for transition in transitions:
            self._emit_transition(transition)
        return len(transitions)

    async def _requeue(
        self, selector: str, selector_params: dict[str, object], *, at: datetime
    ) -> list[UnitTransition]:
        params = {**selector_params, "max_attempts": self._max_attempts}
        pending = await self._run_requeue_branch(
            _sql.REQUEUE_TO_PENDING.format(selector=selector), params, WorkUnitState.PENDING, at
        )
        dead = await self._run_requeue_branch(
            _sql.REQUEUE_TO_DEAD.format(selector=selector), params, WorkUnitState.DEAD, at
        )
        transitions = sorted((*pending, *dead), key=lambda transition: transition.work_unit_id)
        for job_id in {transition.job_id for transition in transitions}:
            await self._recompute_job_state(job_id)
        return transitions

    async def _run_requeue_branch(
        self,
        query: str,
        params: dict[str, object],
        state: WorkUnitState,
        at: datetime,
    ) -> list[UnitTransition]:
        cursor = await self._db.execute(query, params)
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

    # ── status ───────────────────────────────────────────────────────────────

    async def job_status(self, job_id: str) -> JobStatus | None:
        cursor = await self._db.execute(_sql.SELECT_JOB_STATE, {"job_id": job_id})
        job_row = await cursor.fetchone()
        if job_row is None:
            return None
        counts = await self._unit_counts(job_id)
        return JobStatus(
            job_id=job_id,
            state=JobState(str(job_row["state"])),
            total_units=counts.total,
            done_units=counts.done,
            dead_units=counts.dead,
        )

    async def job_details(self, job_id: str) -> JobDetails | None:
        job_cursor = await self._db.execute(_sql.SELECT_JOB_DETAILS, {"job_id": job_id})
        job = await job_cursor.fetchone()
        if job is None:
            return None
        unit_cursor = await self._db.execute(_sql.SELECT_JOB_UNIT_OUTCOMES, {"job_id": job_id})
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
                    result_ref=None if row["result_ref"] is None else str(row["result_ref"]),
                )
                for row in units
            ),
        )

    async def _unit_counts(self, job_id: str) -> UnitCounts:
        cursor = await self._db.execute(_sql.COUNT_JOB_UNITS, {"job_id": job_id})
        row = await cursor.fetchone()
        assert row is not None  # aggregate always returns one row
        return UnitCounts(
            total=int(row["total"]),
            pending=int(row["pending"]),
            leased=int(row["leased"]),
            done=int(row["done"]),
            dead=int(row["dead"]),
        )

    async def _recompute_job_state(self, job_id: str) -> None:
        counts = await self._unit_counts(job_id)
        await self._db.execute(
            _sql.SET_JOB_STATE, {"state": next_job_state(counts), "job_id": job_id}
        )
