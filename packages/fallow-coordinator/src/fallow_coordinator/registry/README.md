# registry — agent registry, auth, model catalogue (module C2)

SQLite-backed (aiosqlite, WAL) source of truth for **who** may talk to the
coordinator and **what** models exist. Async; the coordinator app owns the
connection lifecycle and injects a `now()` clock for deterministic liveness.

## Public API

Re-exported from `fallow_coordinator.registry`:

- `SqliteRegistry(db_path, config: RegistryConfig, now, token_factory=new_token)`
  - lifecycle: `await open()` / `await close()` (or `async with`).
  - tokens: `create_enrollment_token()`, `list_enrollment_tokens()`,
    `revoke_enrollment_token(token_id)`,
    `create_api_key(name, allowlist, rpm_limit, daily_limit)`.
  - agents: `register_agent(RegisterRequest, host) -> RegisterResponse`,
    `record_heartbeat(agent_id, Heartbeat)`, `revoke_agent(agent_id)`.
  - auth: `authenticate_agent(bearer) -> agent_id | None`,
    `authenticate_api_key(bearer) -> ApiKeyInfo | None`.
  - presence fencing and routing: enrollment tokens carry a legacy/site mode; registered agents persist direct/site_relay transport. Presence events advance a monotonic sequence fence and generation; stale heartbeats cannot overwrite routing state.
  - liveness views: `snapshots(now)`, `list_offline(now)`,
    `replica_endpoints(model_id, now)`.
  - models: `put_model(manifest, blob_path)`, `get_manifest`, `get_model`,
    `list_models`, `set_assignments(agent_id, model_ids)`,
    `desired_models(agent_id)`.
  - quota state: `load_quota_snapshots()`, `save_quota_snapshots(snapshots)`.
- `RegistryConfig`, `ApiKeyInfo`, `ApiKeyQuotaSnapshot`, `EnrollmentTokenInfo`,
  `ModelRecord`.
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
- **Revocation is terminal.** `revoke_enrollment_token` spends an unused token
  through the same `used_at` gate an enrolment does, so a revoked join file
  fails identically to a re-used one. `revoke_agent` sets `revoked_at`, after
  which the device token never authenticates again and the agent is absent from
  `snapshots` / `replica_endpoints`: `list_revoked_agents` is where it stays
  visible. Neither has an inverse: a wiped machine re-enrols from a fresh token
  as a new agent. Tokens are named publicly by `token_id`, the first 12 hex
  characters of their stored digest; `normalize_token_id` is the only way in,
  and an id that is not 12 hex characters, or that names more than one
  outstanding token, raises rather than reading as "already spent".
- **A revoked token is not an unknown one.** `authenticate_agent` returns `None`
  for a token this coordinator does not recognise and raises `RevokedAgentError`
  for one whose agent row is revoked. Only the second is terminal, and only the
  second makes a Go agent record the rejection and stay down; a coordinator that
  lost its database rejects every desk with the first, and every desk retries
  (ADR 104).
- `snapshots` / `replica_endpoints` never surface **offline** agents (last
  heartbeat older than `offline_after_s`, default 45s). `list_offline` returns
  exactly those (input for the app's eviction/requeue loop).
- `suspect` = last heartbeat older than `suspect_after_s` (default 15s) but not
  yet offline. `replica_endpoints` only routes to **READY** replicas on
  **non-suspect, IDLE** agents.
- All table names are `registry_`-prefixed so this module may share a database
  file with the queue module without collision.
- API key limits and fixed-interval counter snapshots use the hashed key identity.
  Plaintext keys never enter a snapshot.

## Boundaries

Implements no `fallow_protocol` ABC directly (registries are coordinator-side
infrastructure), but produces/consumes protocol wire types only. No network and
no real clock — tests inject a `FakeClock` and a temp-file database.


Presence sequence fencing applies to `site_relay` transport. The Go Site Mode runtime supplies a restart-monotonic sequence shared by heartbeats and presence events (#118); direct legacy agents retain their existing heartbeat behavior.
