# ADR 057: Coordinator promote (manual warm-standby failover)

Status: accepted · Date: 2026-07-16

## Context

ADR 054 shipped the export half of the warm standby: with `standby_path` set, a
live coordinator writes a consistent snapshot of its state DB (`db_path`, holding
the registry and queue) to that location on a fixed cadence. It deliberately
deferred the other half, in its own words: "No `flw` promote command and no
failover runbook in this PR." Until that lands, a snapshot on the standby host is
inert — an operator has no supported way to bring it into service, so the SPOF is
only half mitigated.

This ADR records the promote half and the three exporter fast-follows that came
out of reviewing the ADR 054 change. It is a separate record rather than an edit
to ADR 054 because the ADRs here are point-in-time decisions: 054 is accepted and
scoped to the export, and this is the follow-up decision it pointed at. Agent
re-pointing (a floating coordinator address or a standby list in agent config)
stays deferred to a later increment; nothing here builds it.

## Decision

### A coordinator-local promote entrypoint, not a `flw` command

`python -m fallow_coordinator promote --config <coordinator.toml>` takes the
snapshot at the config's `standby_path` and installs it as that config's
`db_path`, so a following `serve` resumes from the last exported state. `--snapshot`
overrides the source when the operator placed the file elsewhere; `--force` is the
guard override below.

Promote lives beside the exporter in the coordinator package, not in `flw`,
because the two do different things. `flw` is an HTTP client for the running
coordinator's admin API — it never touches local paths. Promote is offline
filesystem surgery on the coordinator's own state file, done while no coordinator
is running. Putting it in `flw` would drag the state DB layout and local paths
into a package whose whole job is to stay a thin wire client. The existing
`python -m fallow_coordinator serve` entrypoint is the natural home, and promote
reuses the same config loader so both halves read `standby_path` and `db_path`
from one file.

### Validate before installing

Promote refuses to install anything until the snapshot passes three checks: the
file opens as SQLite, `PRAGMA integrity_check` returns `ok`, and it carries the
tables a coordinator state DB must have. The table check is a small curated
sentinel set (`registry_agents`, `registry_models`, `jobs`, `work_units`) rather
than the full schema: it is enough to reject an empty file, a corrupt file, or the
sibling `rag.db` (whose tables are entirely disjoint), without coupling promote to
every future migration.

### Refuse to overwrite a newer db_path

Promote will not overwrite a `db_path` that is at least as new as the snapshot
unless `--force` is passed. A live coordinator writes `db_path` continuously, so a
`db_path` at least as recent as the periodic snapshot is the signal for a
still-running or more-recent primary, and promoting would discard state the
snapshot never captured. An absent or older `db_path` is safe and installs without
a flag. This is a mtime heuristic, not a distributed lock: it catches the two
mistakes that actually happen — running promote against the live DB on a primary
still in service, and promoting a stale snapshot over newer local state — but it
cannot detect a primary running on another host. Failover stays a human decision:
confirm the primary is down, then promote. `--force` is the explicit override once
that is confirmed.

### Install atomically and clear stale WAL sidecars

The validated snapshot is copied to a temp file beside `db_path` and swapped in
with `os.replace`, so a crash mid-install can never leave a half-written state DB.
After the swap, promote deletes any `db_path-wal` and `db_path-shm` left over from
a previous coordinator at that path. This matters: SQLite would otherwise try to
replay an unrelated WAL onto the freshly installed main file. The snapshot from
the backup API is already a complete, self-contained database, so the coordinator
recreates empty sidecars cleanly on its next open.

### Manual only

There is no automatic detection, election, or fencing in this increment, by
design. Removing the total-outage risk needs only a supported manual failover; a
consensus protocol is disproportionate at pilot scale (ADR 054). Promote is one
operator command run with the local coordinator stopped.

### Exporter fast-follows from the ADR 054 review

Three cheap hardening changes land alongside promote, none of which alters the
off-by-default guarantee or the increment-1 export behaviour when on:

- **Clear the temp file before each export.** `_backup_and_replace` now unlinks
  its temp path before opening it. A crashed earlier export could leave an invalid
  temp file, and backing up into it would then fail with "file is not a database"
  on every future run, silently staling the standby.
- **Reject a `standby_path` that collides with `db_path`.** The config now fails
  at load if `standby_path` equals `db_path`, or if the exporter's derived
  `<name>.partial` temp would equal `db_path`. A copy-paste config can no longer
  point the exporter at the live state DB and have it overwrite the file under the
  registry's and queue's open connections.
- **Test the export's concurrent case, not just its aftermath.** The suite now
  interleaves committed writes on the live connection with a running export and
  asserts every write lands and both databases stay intact, proving the live DB is
  writable while the backup is in flight rather than only after it finishes.

## Consequences

- **Failover is a documented two-command step.** On coordinator loss an operator
  runs `promote` on the standby host, then `serve`. Recovery is a restart against
  recent state, bounded by the export interval (ADR 054), not a rebuild.
- **Agent re-pointing is still the missing piece.** Agents target one coordinator
  address, so after a promote someone must point operators and agents at the new
  host. A floating tailnet name or a standby list in agent config is the next
  increment; until then the runbook says to do it by hand.
- **Off-by-default is unchanged.** With `standby_path` unset there is no exporter,
  no new files, and promote is simply a command nobody runs. The new config guard
  only fires when `standby_path` is set.
- **The guard is a heuristic, and honest about it.** It does not fence a live
  primary on another machine. The runbook leads with confirming the primary is
  down, and `--force` is the audited escape hatch for the cases the mtime check
  reads conservatively.
