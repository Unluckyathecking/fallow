# ADR 056: Speculative backup dispatch for batch-job tails (modules C1, C4)

Status: accepted · Date: 2026-07-16

## Context

A batch job finishes when its slowest unit finishes. Near the end a job is down
to a handful of in-flight units, and each is a single point of delay: if the
agent holding one churns — the user comes back, the machine sleeps — the
preemptor yields it (ADR 002) and the unit sits leased-but-dead until its lease
expires and `requeue_expired` puts it back (ADR 011). That is a full
lease-expiry-and-requeue cycle of nothing happening, on the exact unit the whole
job is waiting for.

MapReduce hit the same wall and answered it with backup tasks: near the end of a
phase, schedule a second copy of each still-running task and take whichever
finishes first. That cut its long tail sharply. The scheduler already has the
signal to do this well — the idle-survival model (ADR 022) predicts, per agent,
whether it will stay idle long enough to finish a unit — so we can back up only
the units whose holder is *likely* to churn, not every straggler.

The catch is layering. The mechanism (a second lease on a specific unit) is a
queue concern, but the decision (this holder is about to churn) needs the churn
model, which is scheduler-tier. The module DAG puts `queue` below `scheduler`
and forbids the reverse import, so the decision cannot live in the queue.

## Decision

- **Split the mechanism from the decision along the DAG.** The queue owns a
  crash-safe *backup lease* primitive and the idempotent completion; the scheduler
  owns the pure survival *decision*; the app poll path wires them. The queue never
  imports the churn model, so the layer contract holds.
- **One backup lease per unit, in its own table.** `backup_leases`
  (`work_unit_id` primary key) records a second lease on an already-leased unit.
  The primary key is the bound: a unit gets at most one backup, ever. `lease_backup`
  grants it only when the unit is currently leased to a *different* agent, inside
  the existing transaction and lock. The primary lease is untouched.
- **The backup binds at a reserved attempt, so the primary lane is byte-for-byte
  unchanged.** A backup uploads a real payload, which must be recorded in
  `result_payload_bindings` for downstream ref lookups to find it. That table is
  keyed `(work_unit_id, attempt)`, and the backup shares the primary's logical
  attempt, so a naive write would collide. The backup instead binds at attempt
  `max_attempts + 1` — one past the retry budget, a number no primary attempt for
  that unit can ever reach — authorised by its `backup_leases` row rather than the
  `work_units` lease. Nothing in the primary path changes; the extra lookups only
  run when the primary checks miss, and `backup_leases` is empty when the feature
  is off.
- **First completion wins; the second is a clean no-op.** Finalization is anchored
  where it already was: the `unit_results` primary key plus `INSERT OR IGNORE`.
  Whoever completes first writes the result and marks the unit done through the
  same path as a normal completion — including `MARK_JOB_UNIT_TERMINAL`, so the
  per-job membership snapshot (ADR 054, the fix for the #84 double-count) stays
  honest and a shared unit is never counted twice. The loser hits the
  result-exists short-circuit and returns an idempotent no-op. A backup may also
  salvage a unit whose primary lease already expired to pending, beating a fresh
  re-lease and re-run; it may not resurrect a dead unit.
- **The decision is survival under a threshold, at the tail only.**
  `choose_backup_unit` (`scheduler/speculative.py`, pure) takes the in-flight tail
  units, their holders' snapshots, and the churn model, and returns the unit whose
  holder's modelled survival over the unit's expected runtime is *lowest* and below
  `speculative_survival_threshold` — or nothing. The queue surfaces only tail
  candidates: units in a job with at most `speculative_tail_max_units` unfinished
  units, for a model the polling agent can serve, with no backup yet. A holder
  absent from the fleet view is skipped (no idle age to score).
- **Offered on the idle poll path, off by default.** When an idle agent long-polls
  and `lease_next` finds no pending work — the natural tail signal — the app asks
  the scheduler whether that agent should back up an at-risk unit, and if so calls
  `lease_backup`. Because it only ever uses an agent that was about to get a 204,
  it adds no load to the fleet. `speculative_backup_enabled` defaults to false; the
  survival horizon reuses `churn_est_unit_duration_s`.

## Consequences

- **Off by default is byte-for-byte.** With the flag off, `backup_leases` is never
  written, the extra queue branches are unreachable, and the poll returns exactly
  what it did before. The whole feature is opt-in by construction.
- **Bounded, not blanket, replication.** At most one backup per unit, only at the
  tail, only under the survival threshold, only to an already-idle poller. A
  trusted fleet never double-computes a whole job — the cost the pattern would
  incur if it replicated everything is spent only where the tail is genuinely at
  risk. Tuning is two config numbers, not a code change.
- **The backup only helps when it wins.** If the primary finalizes first, the
  backup's upload is rejected and its compute is discarded — the intended trade:
  spend one redundant run to remove tail latency when a holder is likely to churn.
- **A salvaged churn is not charged as a drop.** When a backup finalizes a unit
  whose holder churned, the holder's lease never expires (the unit went done), so
  the reliability model (ADR 055) does not record the drop it otherwise would.
  Minor, and it errs toward not penalising a machine for a unit that still got
  done.
- **Startup snapshot and full-runtime horizon.** Like ADR 022 the survival model
  is built once at startup and the horizon is the unit's full estimated runtime,
  not its remaining runtime (there is no per-unit `leased_at`). Both bias toward
  backing up sooner, which is the safe direction; refining them is the same future
  work the churn model already carries.
