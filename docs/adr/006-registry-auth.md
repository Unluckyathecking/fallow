# ADR 006: Registry & auth store (module C2)

Status: accepted · Date: 2026-07-15

## Context

The coordinator needs one durable, crash-safe source of truth for enrolled
agents, bearer identities (device tokens, API keys), the model catalogue, and
per-agent model assignments — plus the liveness maths that scheduling and
routing depend on. ADR 000 fixes SQLite (WAL) as the only datastore and hashed
bearer tokens as identity inside a trusted tailnet.

## Decision

- One `SqliteRegistry` (aiosqlite, WAL) with its own `schema.sql`. All tables
  are `registry_`-prefixed so the same database file can later host the queue
  module without name collisions.
- Tokens are `secrets.token_urlsafe(32)`, returned once and stored only as
  sha256 hex; verification re-hashes and compares with `hmac.compare_digest`.
  The bootstrap admin key lives in `RegistryConfig` and authenticates without a
  row (unrestricted allowlist).
- Enrollment tokens are single-use: registration flips `used_at` and inserts
  the agent in a single transaction, rolling back if the token was already
  spent. Protocol-version mismatch is checked first, so a valid token is never
  consumed by a rejected handshake.
- The clock is injected (`now: Callable[[], datetime]`). `snapshots` and
  `replica_endpoints` exclude offline agents (>45s stale) and flag suspect ones
  (>15s); `list_offline` returns the offline set for the app's requeue loop.
  Interactive routing only picks READY replicas on non-suspect IDLE agents.
- Row↔wire conversion goes through pydantic `TypeAdapter`s, so stored JSON can
  never drift from the protocol schema.

## Consequences

- Deterministic, network-free unit tests via a `FakeClock` and a temp-file DB.
- Liveness is derived on read from `last_seen`, not a background sweeper, so
  views are always consistent with the injected clock; the app must call these
  methods with a real clock.
- Sharing a DB file with the queue module is a future option, not yet wired;
  the prefix convention is the only coupling introduced now.
- The admin key is a plaintext config value (tailnet-trusted); rotating it is a
  config change, not a schema migration.
