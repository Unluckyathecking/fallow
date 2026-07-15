-- Durable job / work-unit queue schema (module C1).
-- All timestamps are ISO-8601 UTC strings produced by the injected clock, so
-- lexicographic string comparison is a valid temporal comparison.

CREATE TABLE IF NOT EXISTS jobs (
    job_id      TEXT    PRIMARY KEY,
    kind        TEXT    NOT NULL,
    model_id    TEXT    NOT NULL,
    payload_ref TEXT    NOT NULL,
    params_json TEXT    NOT NULL,
    priority    INTEGER NOT NULL DEFAULT 0,
    state       TEXT    NOT NULL,
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS work_units (
    work_unit_id   TEXT    PRIMARY KEY,
    job_id         TEXT    NOT NULL REFERENCES jobs(job_id),
    idx            INTEGER NOT NULL,
    input_ref      TEXT    NOT NULL,
    est_duration_s REAL,
    state          TEXT    NOT NULL,
    attempts       INTEGER NOT NULL DEFAULT 0,
    lease_agent    TEXT,
    lease_expires  TEXT,
    created_at     TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS unit_results (
    work_unit_id TEXT PRIMARY KEY REFERENCES work_units(work_unit_id),
    status       TEXT NOT NULL,
    result_ref   TEXT,
    error        TEXT,
    metrics_json TEXT,
    agent_id     TEXT NOT NULL,
    completed_at TEXT NOT NULL
);

-- lease_next: scan pending units, join jobs for the model_id filter, order by
-- (priority DESC, created_at, idx). Covering the state + job join keeps it cheap.
CREATE INDEX IF NOT EXISTS ix_work_units_state_job
    ON work_units (state, job_id);

-- requeue_agent: find every unit leased to one agent.
CREATE INDEX IF NOT EXISTS ix_work_units_lease_agent
    ON work_units (lease_agent);

-- requeue_expired: find leased units whose lease has elapsed.
CREATE INDEX IF NOT EXISTS ix_work_units_state_expires
    ON work_units (state, lease_expires);

-- job_status / recompute: aggregate a job's units.
CREATE INDEX IF NOT EXISTS ix_work_units_job
    ON work_units (job_id);

-- lease_next model filter joins on jobs.model_id.
CREATE INDEX IF NOT EXISTS ix_jobs_model
    ON jobs (model_id);
