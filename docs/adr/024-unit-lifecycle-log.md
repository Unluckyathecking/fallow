# ADR 024: Unit lifecycle logging and experiment time

Status: accepted · Date: 2026-07-15

## Context

The failure recovery metric joins two records: a churn event that takes an agent
offline and the later completion of that agent's leased unit on another agent.
The queue previously stored both facts but did not record unit state changes in
the experiment directory.

The two producers also used different clocks. The churn injector recorded
seconds from the start of its replay, while coordinator timestamps were UTC
datetimes. Subtracting those values returns a number, but not a recovery
duration.

## Decision

`UnitTransition` is a frozen protocol type with the unit ID, job ID, agent ID,
attempt, state, and UTC timestamp. `SqliteQueueStore` accepts an optional
synchronous observer and reports successful transitions to `leased`, `done`,
`pending`, and `dead`.

The queue snapshots each transition inside the mutation lock, commits, then
calls the observer after releasing the lock. Observer exceptions are logged and
ignored because a file error after commit cannot undo the database change.
Empty leases, rejected completions, duplicate completions, and requeue calls
that change no rows do not produce records.

The coordinator writes `units.jsonl` next to `events.jsonl`. Each line contains
`work_unit_id`, `job_id`, `agent_id`, `attempt`, `state`, and `t`. The `t` value
is UTC epoch seconds derived from `UnitTransition.at`.

The churn injector now records UTC epoch seconds in `ChurnRecord.t_epoch`
immediately before it performs an action. It keeps `t_executed`, the relative
replay offset, for schedule checks and old analysis inputs. The churn loader
prefers `t_epoch`, accepts the older generic `t` field, and then falls back to
`t_executed`. When `run_meta.json` contains `started_at`, the loader adds that
epoch origin to a relative `t_executed` value. The default analysis input for
unit records is `units.jsonl`.

## Consequences

Failure recovery now compares values on the same time scale. Production runs
must run the coordinator and churn controller on hosts with synchronized UTC
clocks. The experiment runbook should check clock skew before collecting data.

Writing one short JSON line happens after the queue releases its mutation lock.
A failed append does not fail the queue call, so operators must investigate
transition observer errors before accepting a run.

Adding `UnitTransition` changes the shared wire contract. The committed schema
and the later Go agent generator must include it.
