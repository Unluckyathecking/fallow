"""Hand-written SQL statements for the queue store.

Kept in one place so the schema in ``schema.sql`` and the queries that depend on
it stay reviewable side by side. States are interpolated from the protocol enums
at import time (see ``_states``) so there are no magic string literals here.
"""

from typing import Final

from fallow_protocol.messages import JobState, WorkResultStatus, WorkUnitState

# Enum values are the on-disk representation; interpolate them once.
_PENDING: Final[str] = WorkUnitState.PENDING.value
_LEASED: Final[str] = WorkUnitState.LEASED.value
_DONE: Final[str] = WorkUnitState.DONE.value
_DEAD: Final[str] = WorkUnitState.DEAD.value
_SUCCEEDED: Final[str] = WorkResultStatus.SUCCEEDED.value

INSERT_JOB: Final[str] = """
INSERT INTO jobs (job_id, kind, model_id, payload_ref, params_json, priority, state, created_at)
VALUES (:job_id, :kind, :model_id, :payload_ref, :params_json, :priority, :state, :created_at)
"""

# Content-addressed upsert: a re-submitted unit (same work_unit_id) is reattached
# to the new job and its lease/attempt bookkeeping is reset.
UPSERT_WORK_UNIT: Final[str] = """
INSERT INTO work_units
    (work_unit_id, job_id, idx, input_ref, est_duration_s,
     state, attempts, lease_agent, lease_expires, created_at)
VALUES
    (:work_unit_id, :job_id, :idx, :input_ref, :est_duration_s,
     :state, 0, NULL, NULL, :created_at)
ON CONFLICT(work_unit_id) DO UPDATE SET
    job_id         = excluded.job_id,
    idx            = excluded.idx,
    input_ref      = excluded.input_ref,
    est_duration_s = excluded.est_duration_s,
    state          = excluded.state,
    attempts       = 0,
    lease_agent    = NULL,
    lease_expires  = NULL,
    created_at     = excluded.created_at
"""

SELECT_SUCCEEDED_RESULTS: Final[str] = f"""
SELECT work_unit_id FROM unit_results
WHERE status = '{_SUCCEEDED}' AND work_unit_id IN ({{placeholders}})
"""

# Candidate for leasing: highest priority, then oldest, then lowest idx.
SELECT_LEASE_CANDIDATE: Final[str] = f"""
SELECT w.work_unit_id AS work_unit_id,
       w.job_id        AS job_id,
       w.input_ref     AS input_ref,
       w.est_duration_s AS est_duration_s,
       j.kind          AS kind,
       j.model_id      AS model_id
FROM work_units w
JOIN jobs j ON j.job_id = w.job_id
WHERE w.state = '{_PENDING}' AND j.model_id IN ({{placeholders}})
ORDER BY j.priority DESC, w.created_at ASC, w.idx ASC
LIMIT 1
"""

CLAIM_UNIT: Final[str] = f"""
UPDATE work_units
SET state = '{_LEASED}', attempts = attempts + 1,
    lease_agent = :agent_id, lease_expires = :lease_expires
WHERE work_unit_id = :work_unit_id AND state = '{_PENDING}'
RETURNING attempts
"""

EXTEND_LEASES: Final[str] = f"""
UPDATE work_units
SET lease_expires = ?
WHERE state = '{_LEASED}' AND lease_agent = ?
  AND work_unit_id IN ({{placeholders}})
"""

SELECT_UNIT_FOR_COMPLETION: Final[str] = """
SELECT job_id AS job_id, attempts AS attempts,
       lease_agent AS lease_agent, lease_expires AS lease_expires
FROM work_units WHERE work_unit_id = :work_unit_id
"""

SELECT_RESULT_EXISTS: Final[str] = """
SELECT 1 FROM unit_results WHERE work_unit_id = :work_unit_id
"""

INSERT_RESULT: Final[str] = """
INSERT OR IGNORE INTO unit_results
    (work_unit_id, status, result_ref, error, metrics_json, agent_id, completed_at)
VALUES
    (:work_unit_id, :status, :result_ref, :error, :metrics_json, :agent_id, :completed_at)
"""

MARK_UNIT_DONE: Final[str] = f"""
UPDATE work_units SET state = '{_DONE}' WHERE work_unit_id = :work_unit_id
"""

# Requeue branches: the WHERE prefix ({selector}) is supplied by the caller
# (expired-lease selector or by-agent selector); the attempts bound differs.
REQUEUE_TO_PENDING: Final[str] = f"""
UPDATE work_units SET state = '{_PENDING}'
WHERE {{selector}} AND attempts < :max_attempts
RETURNING work_unit_id, job_id, lease_agent, attempts
"""

REQUEUE_TO_DEAD: Final[str] = f"""
UPDATE work_units SET state = '{_DEAD}'
WHERE {{selector}} AND attempts >= :max_attempts
RETURNING work_unit_id, job_id, lease_agent, attempts
"""

SELECTOR_EXPIRED: Final[str] = f"state = '{_LEASED}' AND lease_expires < :now"
SELECTOR_BY_AGENT: Final[str] = f"state = '{_LEASED}' AND lease_agent = :agent_id"

SELECT_JOB_STATE: Final[str] = "SELECT state FROM jobs WHERE job_id = :job_id"

COUNT_JOB_UNITS: Final[str] = f"""
SELECT
    COUNT(*)                                    AS total,
    COALESCE(SUM(state = '{_PENDING}'), 0)      AS pending,
    COALESCE(SUM(state = '{_LEASED}'), 0)       AS leased,
    COALESCE(SUM(state = '{_DONE}'), 0)         AS done,
    COALESCE(SUM(state = '{_DEAD}'), 0)         AS dead
FROM work_units WHERE job_id = :job_id
"""

SET_JOB_STATE: Final[str] = "UPDATE jobs SET state = :state WHERE job_id = :job_id"

# Job lifecycle values, exported for the store's recompute logic.
JOB_PENDING: Final[str] = JobState.PENDING.value
JOB_RUNNING: Final[str] = JobState.RUNNING.value
JOB_DONE: Final[str] = JobState.DONE.value
