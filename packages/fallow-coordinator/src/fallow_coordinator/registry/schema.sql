-- Registry / auth schema (module C2).
-- Table names are prefixed `registry_` so this module can share a database
-- file with the queue module without colliding on names.

CREATE TABLE IF NOT EXISTS registry_agents (
    agent_id           TEXT    PRIMARY KEY,
    hostname           TEXT    NOT NULL,
    host               TEXT    NOT NULL,
    caps_json          TEXT    NOT NULL,
    device_token_hash  TEXT    NOT NULL,
    state              TEXT    NOT NULL,
    last_seen          TEXT    NOT NULL,
    user_idle_s        REAL    NOT NULL DEFAULT 0,
    mem_available_mb   INTEGER NOT NULL DEFAULT 0,
    gpus_json          TEXT    NOT NULL DEFAULT '[]',
    replicas_json      TEXT    NOT NULL DEFAULT '[]',
    serving_paused     INTEGER NOT NULL DEFAULT 0,
    predicted_idle_remaining_s REAL,
    predicted_idle_confidence  REAL,
    registered_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_registry_agents_token
    ON registry_agents (device_token_hash);

CREATE TABLE IF NOT EXISTS registry_enrollment_tokens (
    token_hash  TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    used_at     TEXT
);

CREATE TABLE IF NOT EXISTS registry_api_keys (
    key_hash              TEXT PRIMARY KEY,
    name                  TEXT NOT NULL,
    model_allowlist_json  TEXT,
    rpm_limit             INTEGER CHECK (rpm_limit IS NULL OR rpm_limit > 0),
    daily_limit           INTEGER CHECK (daily_limit IS NULL OR daily_limit > 0),
    created_at            TEXT NOT NULL,
    revoked_at            TEXT
);

CREATE TABLE IF NOT EXISTS registry_api_key_quota_snapshots (
    key_hash           TEXT PRIMARY KEY REFERENCES registry_api_keys(key_hash) ON DELETE CASCADE,
    bucket_tokens      REAL    NOT NULL,
    bucket_updated_at  TEXT    NOT NULL,
    day                TEXT    NOT NULL,
    daily_count        INTEGER NOT NULL,
    snapshotted_at     TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS registry_models (
    model_id       TEXT    PRIMARY KEY,
    manifest_json  TEXT    NOT NULL,
    blob_path      TEXT    NOT NULL,
    enabled        INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS registry_assignments (
    model_id  TEXT NOT NULL,
    agent_id  TEXT NOT NULL,
    PRIMARY KEY (model_id, agent_id)
);
