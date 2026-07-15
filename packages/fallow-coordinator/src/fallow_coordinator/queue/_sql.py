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

DELETE_INCOMPLETE_RESULT_BINDINGS: Final[str] = """
DELETE FROM result_payload_bindings
WHERE work_unit_id = :work_unit_id
"""

DELETE_NON_SUCCEEDED_RESULT: Final[str] = f"""
DELETE FROM unit_results
WHERE work_unit_id = :work_unit_id AND status != '{_SUCCEEDED}'
"""

SELECT_SUCCEEDED_RESULTS: Final[str] = f"""
SELECT r.work_unit_id, r.result_ref
FROM unit_results r
WHERE r.status = '{_SUCCEEDED}'
  AND r.result_ref IS NOT NULL
  AND r.work_unit_id IN ({{placeholders}})
  AND EXISTS (
      SELECT 1 FROM result_payload_bindings b
      WHERE b.work_unit_id = r.work_unit_id
        AND b.agent_id = r.agent_id
        AND b.result_ref = r.result_ref
  )
"""

BACKFILL_JOB_UNIT_MEMBERSHIPS: Final[str] = f"""
INSERT OR IGNORE INTO job_unit_memberships
    (job_id, work_unit_id, idx, input_ref, terminal_state, result_status, result_ref)
SELECT w.job_id, w.work_unit_id, w.idx, w.input_ref,
       CASE WHEN w.state IN ('{_DONE}', '{_DEAD}') THEN w.state ELSE NULL END,
       CASE WHEN w.state = '{_DONE}' THEN r.status ELSE NULL END,
       CASE WHEN w.state = '{_DONE}' AND r.status = '{_SUCCEEDED}' AND EXISTS (
                SELECT 1 FROM result_payload_bindings b
                WHERE b.work_unit_id = r.work_unit_id
                  AND b.agent_id = r.agent_id
                  AND b.result_ref = r.result_ref
            ) THEN r.result_ref ELSE NULL END
FROM work_units w
LEFT JOIN unit_results r ON r.work_unit_id = w.work_unit_id
"""

SNAPSHOT_TERMINAL_OWNER: Final[str] = f"""
INSERT INTO job_unit_memberships
    (job_id, work_unit_id, idx, input_ref, terminal_state, result_status, result_ref)
SELECT w.job_id, w.work_unit_id, w.idx, w.input_ref, w.state,
       CASE WHEN w.state = '{_DONE}' THEN r.status ELSE NULL END,
       CASE WHEN w.state = '{_DONE}' AND r.status = '{_SUCCEEDED}' AND EXISTS (
                SELECT 1 FROM result_payload_bindings b
                WHERE b.work_unit_id = r.work_unit_id
                  AND b.agent_id = r.agent_id
                  AND b.result_ref = r.result_ref
            ) THEN r.result_ref ELSE NULL END
FROM work_units w
LEFT JOIN unit_results r ON r.work_unit_id = w.work_unit_id
WHERE w.work_unit_id = :work_unit_id AND w.state IN ('{_DONE}', '{_DEAD}')
ON CONFLICT(job_id, work_unit_id) DO UPDATE SET
    terminal_state = excluded.terminal_state,
    result_status = excluded.result_status,
    result_ref = excluded.result_ref
WHERE job_unit_memberships.terminal_state IS NULL
"""

INSERT_JOB_UNIT_MEMBERSHIP: Final[str] = """
INSERT INTO job_unit_memberships
    (job_id, work_unit_id, idx, input_ref, terminal_state, result_status, result_ref)
VALUES
    (:job_id, :work_unit_id, :idx, :input_ref, :terminal_state, :result_status, :result_ref)
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

SELECT_RESULT_UPLOAD_ATTEMPT: Final[str] = f"""
SELECT attempts
FROM work_units
WHERE work_unit_id = :work_unit_id
  AND state = '{_LEASED}'
  AND lease_agent = :agent_id
"""

BIND_RESULT_PAYLOAD: Final[str] = f"""
INSERT INTO result_payload_bindings
    (work_unit_id, agent_id, attempt, digest, result_ref, accepted_at)
SELECT work_unit_id, :agent_id, :attempt, :digest, :result_ref, :accepted_at
FROM work_units
WHERE work_unit_id = :work_unit_id
  AND state = '{_LEASED}'
  AND lease_agent = :agent_id
  AND attempts = :attempt
ON CONFLICT(work_unit_id, attempt) DO NOTHING
RETURNING 1
"""

SELECT_MATCHING_RESULT_BINDING: Final[str] = f"""
SELECT 1
FROM result_payload_bindings b
JOIN work_units w ON w.work_unit_id = b.work_unit_id
WHERE b.work_unit_id = :work_unit_id
  AND b.agent_id = :agent_id
  AND b.attempt = :attempt
  AND b.digest = :digest
  AND b.result_ref = :result_ref
  AND w.state = '{_LEASED}'
  AND w.lease_agent = b.agent_id
  AND w.attempts = b.attempt
"""

SELECT_UNIT_FOR_COMPLETION: Final[str] = """
SELECT job_id AS job_id, state AS state, attempts AS attempts,
       lease_agent AS lease_agent, lease_expires AS lease_expires
FROM work_units WHERE work_unit_id = :work_unit_id
"""

SELECT_RESULT_BINDING_FOR_COMPLETION: Final[str] = """
SELECT 1
FROM result_payload_bindings
WHERE work_unit_id = :work_unit_id
  AND agent_id = :agent_id
  AND attempt = :attempt
  AND result_ref = :result_ref
"""

SELECT_COMPLETED_RESULT_REF: Final[str] = f"""
SELECT r.result_ref
FROM unit_results r
JOIN result_payload_bindings b
  ON b.work_unit_id = r.work_unit_id
 AND b.agent_id = r.agent_id
 AND b.result_ref = r.result_ref
WHERE r.work_unit_id = :work_unit_id
  AND r.status = '{_SUCCEEDED}'
  AND r.result_ref IS NOT NULL
LIMIT 1
"""

SELECT_JOB_DETAILS: Final[str] = """
SELECT model_id, params_json FROM jobs WHERE job_id = :job_id
"""

SELECT_ACTIVE_JOBS_FOR_UNITS: Final[str] = f"""
SELECT DISTINCT j.job_id, j.kind, j.model_id, j.payload_ref, j.params_json, j.priority
FROM work_units u
JOIN jobs j ON j.job_id = u.job_id
WHERE u.work_unit_id IN ({{placeholders}})
  AND u.state IN ('{_PENDING}', '{_LEASED}')
"""

SELECT_JOB_UNIT_OUTCOMES: Final[str] = """
SELECT m.work_unit_id, m.idx, m.input_ref,
       COALESCE(m.terminal_state, u.state) AS state,
       m.result_status, m.result_ref
FROM job_unit_memberships m
LEFT JOIN work_units u
  ON u.work_unit_id = m.work_unit_id AND u.job_id = m.job_id
WHERE m.job_id = :job_id
ORDER BY m.idx, m.work_unit_id
"""

SELECT_RESULT_EXISTS: Final[str] = """
SELECT 1 FROM unit_results WHERE work_unit_id = :work_unit_id
"""

SELECT_MATCHING_COMPLETION: Final[str] = """
SELECT 1
FROM unit_results r
JOIN work_units w ON w.work_unit_id = r.work_unit_id
WHERE r.work_unit_id = :work_unit_id
  AND r.status = :status
  AND r.result_ref IS :result_ref
  AND r.error IS :error
  AND r.metrics_json IS :metrics_json
  AND r.agent_id = :agent_id
  AND w.lease_agent = :agent_id
  AND w.attempts = :attempt
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

MARK_JOB_UNIT_TERMINAL: Final[str] = """
UPDATE job_unit_memberships
SET terminal_state = :terminal_state,
    result_status = :result_status,
    result_ref = :result_ref
WHERE job_id = :job_id AND work_unit_id = :work_unit_id
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
    COUNT(*)                                               AS total,
    COALESCE(SUM(COALESCE(m.terminal_state, u.state) = '{_PENDING}'), 0) AS pending,
    COALESCE(SUM(COALESCE(m.terminal_state, u.state) = '{_LEASED}'), 0)  AS leased,
    COALESCE(SUM(COALESCE(m.terminal_state, u.state) = '{_DONE}'), 0)    AS done,
    COALESCE(SUM(COALESCE(m.terminal_state, u.state) = '{_DEAD}'), 0)    AS dead
FROM job_unit_memberships m
LEFT JOIN work_units u
  ON u.work_unit_id = m.work_unit_id AND u.job_id = m.job_id
WHERE m.job_id = :job_id
"""

SELECT_JOB_FINALIZATION: Final[str] = """
SELECT indexed_items FROM job_finalizations WHERE job_id = :job_id
"""

INSERT_JOB_FINALIZATION: Final[str] = """
INSERT INTO job_finalizations (job_id, indexed_items)
VALUES (:job_id, :indexed_items)
ON CONFLICT(job_id) DO NOTHING
"""

SET_JOB_STATE: Final[str] = "UPDATE jobs SET state = :state WHERE job_id = :job_id"

# Job lifecycle values, exported for the store's recompute logic.
JOB_PENDING: Final[str] = JobState.PENDING.value
JOB_RUNNING: Final[str] = JobState.RUNNING.value
JOB_DONE: Final[str] = JobState.DONE.value
