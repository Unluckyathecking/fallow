# ADR 054: Coordinator warm standby

**Status:** Accepted

**Date:** 2026-07-16

## Context

ADR 000 records one deliberate reliability gap: "Single coordinator is a SPOF;
acceptable at <=50 machines, revisit for HA later." A dead coordinator takes the
whole fabric down. For a school pilot that means the AI service is simply gone
until someone restarts the process. With the core roadmap done, this is the
highest-value reliability item left.

The coordinator's durable state is one WAL-mode SQLite file, `db_path`, opened by
both the registry and the queue (the RAG vectors live in a separate `rag.db` and
are rebuildable by re-ingestion, so they are not the SPOF-critical state). If we
have a recent, consistent copy of `db_path` on a second machine, a failed
coordinator becomes a restart on the standby rather than an outage.

## Decision

Warm standby, not a consensus cluster. At <=50 machines a Raft/etcd quorum is
disproportionate — it adds a distributed-systems dependency and failure modes far
heavier than the problem. The state is a single SQLite file; the lightweight path
is to keep a recent consistent copy of it somewhere else.

This increment ships the **state-export half** only:

- A background loop, off unless `standby_path` is set, writes a consistent
  snapshot of `db_path` to `standby_path` every `standby_export_interval_s`
  (default 60s). With `standby_path` unset the coordinator behaves exactly as
  before — no loop, no new files.
- The snapshot is produced with the SQLite **online backup API**
  (`sqlite3.Connection.backup`) against a *separate* connection to the live file.
  That API reads a transactionally consistent view of a database other
  connections are still writing, so it never locks or corrupts the live DB. The
  copy is written to a temp file and atomically renamed into place, so a crash
  mid-export can never leave a partial snapshot at the destination. The blocking
  copy runs in a worker thread.

Failover is **manual** for this first cut: on coordinator loss an operator points
a coordinator at the shipped snapshot and starts it. The promote command and the
runbook are a follow-up, not this PR.

### Mechanism: backup API over a dependency

We considered Litestream (continuous WAL streaming, battle-tested) against a small
in-repo export. The pillars — simplicity and cost-effectiveness before
competency — favour no new dependency unless it earns its place:

- The stdlib backup API is already present, is the documented way to copy a live
  SQLite DB, and needs no new process, config, or supervision. A periodic
  checkpoint-style copy is enough for a warm standby whose failover is manual and
  measured in minutes, not seconds.
- Litestream buys a smaller recovery-point window (seconds of streamed WAL vs one
  export interval) and point-in-time restore. Neither is worth a new dependency
  and a second daemon to run and monitor at this scale. If the RPO ever needs to
  be sub-interval, this ADR can be revisited — the export is deliberately a
  self-contained module that Litestream could replace without touching callers.

The tradeoff is an explicit bounded window: up to one `standby_export_interval_s`
of the most recent committed state can be lost on failover. That is acceptable
for the pilot and documented honestly.

## Explicitly deferred

- **Promotion.** No `flw` promote command and no failover runbook in this PR.
- **Agent re-pointing.** Agents still target one coordinator address; the standby
  fallback (floating tailnet name or a standby list in agent config) is a
  separate change.
- **Automatic detection / election.** Failover is a human decision for now.
- **`rag.db`.** Rebuildable by re-ingestion; add to the export later if wanted.

These are follow-up increments, tracked under the same issue. This PR does not
build any half of them.

## Consequences

- The single-coordinator SPOF is mitigated, not removed: a recent consistent copy
  of the state exists off-box, so recovery is a restart rather than a rebuild.
- Recovery point is bounded by the export interval; recovery is a manual step
  until the promote path lands.
- Behaviour is unchanged for every existing deployment (feature off by default).

## Verification

Unit tests cover the export against a live WAL database: the snapshot is openable
and holds the committed rows; the live connection keeps reading and writing
during and after the export (proving it is neither locked nor corrupted); the
write lands atomically with no `.partial` file left behind; and the loop, driven
by an injected clock, produces a snapshot and stops cleanly. Config tests assert
the feature is off by default and that `standby_path` / `standby_export_interval_s`
load from TOML and the environment.
