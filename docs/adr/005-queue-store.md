# ADR 005: Durable job/work-unit queue (module C1)

Status: accepted · Date: 2026-07-15

## Context

The coordinator needs a crash-safe store for batch jobs, their content-addressed
work units, leasing to agents, retries, and results. ADR 000 fixes SQLite (WAL)
as the only datastore and makes coordinator stores async (aiosqlite). Units are
idempotent and content-addressed, so duplicate/late completions and re-submits
must be harmless.

## Decision

- Implement `QueueStore` as `SqliteQueueStore` over one aiosqlite connection with
  hand-written SQL (no ORM). Schema lives in `queue/schema.sql`, applied by an
  async `init()` alongside `PRAGMA journal_mode=WAL` + `busy_timeout`.
- **Time is injected.** A `now()` callable is the sole clock; timestamps are
  stored as fixed-width ISO-8601 UTC strings so lexicographic order == time order.
- **One `asyncio.Lock` guards every mutation.** On a single-threaded event loop
  with one connection, this makes each read-then-write sequence atomic, so
  `lease_next` cannot double-lease. `lease_next` still uses an
  `UPDATE ... WHERE state='pending' RETURNING` claim as defence in depth.
- **Idempotency by construction.** `unit_results` is keyed by `work_unit_id`
  (INSERT OR IGNORE); completion is accepted only from the lease holder or for an
  already-expired lease. Dedup on submit: a unit with a stored `SUCCEEDED` result
  is inserted `DONE`; re-submitted unit ids upsert-reattach to the new job.
- **Retries** live in `requeue_expired`/`requeue_agent`: `attempts` is bumped at
  lease time; requeue routes `pending` (< max) or `dead` (≥ max) and recomputes
  job completion.

## Consequences

- Correct and simple, but the single lock serializes all writes — fine for one
  coordinator at ≤50 machines (ADR 000); revisit if write throughput bites.
- `work_unit_id` is treated as an opaque global PK. A genuine re-submit of the
  same ids reattaches those unit rows to the new job, so the prior job's unit
  counts drop — acceptable because content-addressed ids only collide on a true
  identical re-submit (see open question in the handoff about `job_id` in the
  content hash).
- `SqliteQueueStore` inherits the ABC, so signature drift fails type-checking.
