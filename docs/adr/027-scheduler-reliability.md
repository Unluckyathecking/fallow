# ADR 027: Per-agent task-success reliability in scheduling (module C7)

Status: accepted · Date: 2026-07-16

## Context

The churn-aware scheduler (ADR 022) ranks eligible agents by one number: the
modelled probability a machine stays idle long enough to finish a unit. That is
an idle-survival estimate. It is silent on whether an agent actually *completes*
the work it leases.

The two are not the same. A machine can be reliably idle and still be a poor
place to park a unit: it crashes mid-run, its agent process dies, or it churns in
a way the model underweights, and the lease expires. When that happens the unit
is requeued (ADR 011 `requeue_expired`) or, once retries are spent, marked dead
(ADR 024). Idle-reliable, task-unreliable. Volunteer computing has tracked host
reliability for exactly this reason for years; BOINC keeps a per-host success
record and steers work toward hosts that return results. We already emit the raw
signal — the unit lifecycle log (`units.jsonl`, ADR 024) records every
transition with its `agent_id` — but nothing in scheduling reads it.

## Decision

- **A per-agent success rate from the lifecycle log.** `ReliabilityModel`
  (`reliability.py`, frozen) tallies each agent's terminal unit outcomes:
  `reliability(agent) = completed / (completed + dropped)`. A `DONE` transition is
  a completion; a requeue (`PENDING`) or a `DEAD` transition is a drop, charged to
  the agent that held the lease. A `LEASED` transition is a handout, not a
  terminal outcome, so it counts toward neither.
- **Optimistic prior for unseen agents.** An agent with no terminal outcome on
  record scores a configurable prior (default 0.9), the same stance `ChurnModel`
  takes for unseen agents: a new machine is not starved of work for lack of a
  track record. There is no extra smoothing — a thin sample scores its raw rate.
  It can afford to, because reliability's influence is bounded (below).
- **Pure builder, no I/O.** `build_reliability_model(transitions)` consumes
  already-parsed `units.jsonl` mappings; the app layer owns the file read. Rows
  missing a string `agent_id` or carrying an unrecognised `state` are skipped, so
  a half-written log line never derails the build. The model is pure data, so a
  fixed log yields a fixed model — the ADR 022 replay-determinism contract holds.
- **Folded in as a bounded secondary weight, not a takeover.** `ChurnAwareScheduler`
  keeps idle-survival as its primary signal. The placement score becomes
  `survival + reliability_weight * reliability`, ranked descending, with the v1
  capability order (warm replica → GPU → free RAM → `agent_id`) unchanged as the
  tiebreak. Both survival and reliability are in `[0, 1]` and the weight is small
  (default 0.1), which gives a clean guarantee: an agent trailing on survival by
  the weight or more can never be promoted by reliability, because reliability can
  add at most `reliability_weight` to a score. Reliability only shapes genuine
  near-ties — agents whose survival is within the weight of each other. So a
  survival lead is decisive and reliability is the tie-shaper the primary signal
  asked for.
- **Wiring parallels churn.** The `churn_v2` arm builds the reliability model once
  at startup from the prior run's `units.jsonl` (a sibling of `events.jsonl`), the
  same way it builds the churn model from the churn history. The reliability model
  is optional on the policy: when absent it contributes nothing and the arm ranks
  on pure idle-survival, identical to ADR 022. No config knob for the weight —
  the module default stands until an experiment shows it should move.

## Consequences

- **Only churn_v2 changes.** `CapabilityScheduler` and `RoundRobinScheduler` are
  untouched, and churn_v2 with no reliability model reproduces its old ranking
  exactly. The new dimension is opt-in by construction.
- **Startup snapshot, not live.** Like the churn model, the reliability model is
  built once at startup from the log on disk and does not refresh from in-run
  transitions. Fine for a bounded experiment run (restart between arms); an
  online rebuild is the same future work the churn model already carries.
- **Drops are attributed, not diagnosed.** A requeue charged to an agent may have
  been its fault (crash, churn) or not (a transient coordinator hiccup). The rate
  treats them alike. Over a run the noise averages out, and the bounded weight
  caps how much any single misattribution can move a decision.
- **The weight encodes "comparable".** Choosing 0.1 defines the near-tie band
  reliability is allowed to reorder. It is a deliberate, documented lever; tuning
  it is a config or experiment question, not a code change to the ranking.
