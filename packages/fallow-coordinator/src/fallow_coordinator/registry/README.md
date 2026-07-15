# registry — agent registry, auth, model catalogue (module C2)

SQLite-backed (aiosqlite, WAL) source of truth for **who** may talk to the
coordinator and **what** models exist. Async; the coordinator app owns the
connection lifecycle and injects a `now()` clock for deterministic liveness.

## Public API

Re-exported from `fallow_coordinator.registry`:

- `SqliteRegistry(db_path, config: RegistryConfig, now, token_factory=new_token)`
  - lifecycle: `await open()` / `await close()` (or `async with`).
  - tokens: `create_enrollment_token()`, `create_api_key(name, allowlist)`.
  - agents: `register_agent(RegisterRequest, host) -> RegisterResponse`,
    `record_heartbeat(agent_id, Heartbeat)`.
  - auth: `authenticate_agent(bearer) -> agent_id | None`,
    `authenticate_api_key(bearer) -> ApiKeyInfo | None`.
  - liveness views: `snapshots(now)`, `list_offline(now)`,
    `replica_endpoints(model_id, now)`.
  - models: `put_model(manifest, blob_path)`, `get_manifest`, `get_model`,
    `list_models`, `set_assignments(agent_id, model_ids)`,
    `desired_models(agent_id)`.
- `RegistryConfig`, `ApiKeyInfo`, `ModelRecord`.
- Errors: `RegistryError`, `RegistryNotOpenError`, `ProtocolMismatchError`,
  `EnrollmentTokenError`, `UnknownAgentError`.

## Invariants

- Every token is `secrets.token_urlsafe(32)`, returned to the caller once and
  stored only as its sha256 hex digest. Verification re-hashes and compares in
  constant time.
- **Enrollment tokens are single-use.** Consumption flips `used_at` and inserts
  the agent in one transaction; a used or unknown token raises
  `EnrollmentTokenError`. Protocol-version mismatch is rejected *before* the
  token is consumed, so a good token is never wasted on a bad handshake.
- `snapshots` / `replica_endpoints` never surface **offline** agents (last
  heartbeat older than `offline_after_s`, default 45s). `list_offline` returns
  exactly those (input for the app's eviction/requeue loop).
- `suspect` = last heartbeat older than `suspect_after_s` (default 15s) but not
  yet offline. `replica_endpoints` only routes to **READY** replicas on
  **non-suspect, IDLE** agents.
- All table names are `registry_`-prefixed so this module may share a database
  file with the queue module without collision.

## Boundaries

Implements no `fallow_protocol` ABC directly (registries are coordinator-side
infrastructure), but produces/consumes protocol wire types only. No network and
no real clock — tests inject a `FakeClock` and a temp-file database.
