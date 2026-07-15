# queue — durable job / work-unit store (coordinator module C1)

SQLite-backed implementation of `fallow_protocol.interfaces.QueueStore`. It is
the single source of truth for batch jobs, their content-addressed work units,
leasing, retries, and results. WAL mode + per-mutation commits make it
crash-safe.

## Public API

Re-exported from `fallow_coordinator.queue`:

- `SqliteQueueStore(db_path, now=<aware-UTC-now>, max_attempts=4, default_lease_s=120.0)`
- `QueueNotInitializedError`

```python
store = SqliteQueueStore(path, now=clock)
await store.init()                       # opens conn, sets pragmas, creates schema
job_id = await store.submit_job(job, units)
lease = await store.lease_next(agent_id, ["model-a"])   # WorkUnitLease | None
await store.extend_leases(agent_id, [lease.work_unit_id])
await store.complete_unit(agent_id, result)
n = await store.requeue_expired()        # lease-timeout sweep
n = await store.requeue_agent(agent_id)  # offline-agent eviction
status = await store.job_status(job_id)  # JobStatus | None
await store.close()
```

`init()` must be awaited before any other call (else `QueueNotInitializedError`).

## Behaviour / invariants

- **Injected clock.** Every timestamp comes from the `now` callable and is stored
  as a fixed-width ISO-8601 UTC string, so lexicographic comparison equals
  chronological comparison. Nothing calls the wall clock directly.
- **Atomic leasing.** `lease_next` picks one `pending` unit runnable with one of
  the given `model_ids`, ordered by `priority DESC, created_at, idx`, flips it to
  `leased`, and bumps `attempts`. All mutators run under one `asyncio.Lock` on a
  single connection, so the same unit is never leased twice (proven by the
  concurrency test: 10 concurrent callers, 5 units → 5 distinct leases).
- **Lease window.** `lease_expires = now + max(2·est_duration_s, default_lease_s)`.
  `attempt` (returned in the lease, ≥1) is the post-increment attempt count.
- **`input_url` passthrough.** The lease's `input_url` is the unit's stored
  `input_ref` verbatim; the HTTP layer turns it into a fetchable URL.
- **Idempotent completion.** `unit_results` is keyed by `work_unit_id`, so
  duplicate/late completions are silent no-ops. A completion is accepted only
  from the current lease holder, or for an already-expired lease (any agent) — a
  valid lease held by another agent cannot be clobbered.
- **Content-addressed dedup.** On `submit_job`, a unit whose `work_unit_id`
  already has a `SUCCEEDED` result is inserted as `DONE`; a job can complete
  instantly. Re-submitting the same unit ids reattaches them to the new job.
- **Retries.** `requeue_expired` / `requeue_agent` move `leased` units back to
  `pending` while `attempts < max_attempts`, else to `dead`. A job becomes `DONE`
  once no unit is `pending` or `leased`.
- **No scheduling policy here.** Eligibility beyond `model_id` matching is the
  scheduler's concern.

## Files

- `schema.sql` — tables + indexes (executed by `init()`).
- `store.py` — `SqliteQueueStore`.
- `_sql.py` — hand-written SQL (no ORM); states interpolated from protocol enums.
- `_serialization.py`, `_jobstate.py`, `_constants.py` — pure helpers/config.
