# ADR 022: Churn-aware scheduler v2 (module C6, experiment arm c v2)

Status: accepted · Date: 2026-07-15

## Context

The Wave-4 experiment compares three batch-placement arms: (a) single dedicated
machine, (b) round-robin (`RoundRobinScheduler`, ADR 011), (c) capability-aware
(`CapabilityScheduler`, ADR 011). All three ignore *when the user is likely to
come back*. On an office fleet that is the dominant cost: a batch unit parked on a
machine whose user returns mid-flight is preempted (instant yield, ADR 002), its
lease expires, and the unit is requeued (ADR 011 `requeue_expired`): a wasted
run plus a recovery-time hit, exactly the metrics Wave 4 measures.

We already record the signal needed to predict this. Every agent emits `user_idle`
and `user_returned` events to the coordinator's `events.jsonl` (ADR 014). The gap
between a paired `user_idle` → `user_returned` is one *completed idle session*.
This ADR adds arm (c) v2: a scheduler that ranks eligible agents by the modelled
probability they stay idle long enough to finish a unit.

## Decision

- **Empirical survival, not Kaplan–Meier.** Every idle session in the log is
  *completed* (both endpoints observed), so the raw empirical distribution of
  session lengths is exact: no censoring, no KM estimator. `ChurnModel`
  (`churn_model.py`, frozen) stores per-`(agent_id, hour-of-day)` sorted session
  lengths and computes
  `survival(u, t) = |{s ≥ u+t}| / |{s ≥ u}|`: of the sessions that reached the
  agent's *current* idle age `u`, the fraction that reached `u + t`. Conditioning
  on `u` (the live `AgentSnapshot.user_idle_s`) credits a machine its accumulated
  idle tenure.
- **Diurnal buckets with cascade fallback.** Office churn is strongly
  hour-of-day dependent (a 09:00 idle is a coffee; a 12:30 idle is lunch), so the
  model buckets by hour. A thin bucket (`< 5` sessions) falls back to the agent's
  all-hours pool; an agent with no history at all falls back to a configurable
  **optimistic prior** (default 0.9) so a never-seen machine is not starved of
  work for lack of data. `hour-of-day` is supplied by an injected function, never
  read from a wall clock.
- **Pure builder, no I/O.** `build_churn_model(events, hour_of)` consumes already
  parsed event mappings and pairs `user_idle` → `user_returned` per agent in file
  (chronological) order; unpaired returns, re-opened idles, negative-length pairs,
  and malformed lines are skipped. The app layer owns the file read, so the model
  and the policy are replay-deterministic (a fixed log + fixed `hour_fn` ⇒ fixed
  decisions), matching the ADR 011 purity contract for arms.
- **Ranking = churn first, v1 order as tiebreak.** `ChurnAwareScheduler`
  (`v2.py`) filters to eligible agents exactly as v1 (`IDLE`, not `suspect`,
  GPU-capable when required, since the correctness gate is unchanged and shared via
  `_eligibility`), then ranks by highest `P(stays idle ≥ est_unit_duration_s)`,
  breaking ties with the exact v1 capability order (warm replica → any GPU → most
  free RAM → `agent_id`). So with no churn data the arm degrades gracefully to v1.
- **`pick_replica` is unchanged from v1 (least-inflight).** Churn ranking is a
  *batch-placement* win only. Interactive requests are short; a mid-stream yield
  truncates at most one response (ADR 000) and there is no lease to requeue, so
  predicting churn buys nothing on the interactive path. That path is
  latency-sensitive, and we must not add work to it.
- **Config selects the arm.** `CoordinatorConfig.scheduler ∈ {capability,
  roundrobin, churn_v2}` (default `capability`, so existing behaviour is
  untouched) plus `churn_est_unit_duration_s` (default 60.0). The factory builds
  the chosen policy; for `churn_v2` it reads `events.jsonl` once at startup and
  injects `hour_fn = clock().hour`. `CoordinatorState.policy` is widened from the
  concrete `CapabilityScheduler` to the `SchedulerPolicy` abstraction so all three
  arms fit (the state already depended on the abstraction in practice).

## Consequences

- **Startup snapshot, not live.** The model is built once at startup from the log
  on disk. A long-running coordinator's model goes stale; there is no in-process
  refresh from new events yet. Acceptable for a bounded Wave-4 experiment run
  (restart between arms); periodic/online rebuild is future work.
- **Cold start = v1.** With an empty/missing log every agent scores the optimistic
  prior, so ties everywhere and the arm is exactly v1 until sessions accumulate:
  a deliberate, safe default.
- **Estimated, not per-unit, horizon.** Ranking uses one configured
  `est_unit_duration_s` rather than each unit's `WorkUnitSpec.est_duration_s`;
  v0.1 handout is PULL-based (ADR 011) so `select_agent` does not see the concrete
  unit. Threading per-unit duration through is a natural v3 refinement.
- **Determinism preserved.** Because the model is pure data and the only clock
  read is the injected `hour_fn`, the whole arm remains replayable from a fixed
  event log. The bench requirement holds.
