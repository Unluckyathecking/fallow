# ADR 011: Scheduler v1 policies + dispatch loop (module C4)

Status: accepted · Date: 2026-07-15

## Context

The coordinator must decide which idle agent receives a batch unit and which
replica serves an interactive request. This is the experiment surface: arms must
be swappable and results reproducible, so the decision logic must be pure
(`SchedulerPolicy` in ADR 000's interface layer — no I/O, no clocks, no
randomness). Batch handout in v0.1 is PULL-based (ADR 000 §2: agents long-poll;
the coordinator never dials an agent except to proxy inference).

## Decision

- **Two pure arms.** `CapabilityScheduler` (arm c, the v1 default) filters to
  eligible agents — `IDLE`, not `suspect`, GPU-capable when `needs_gpu` — then
  ranks by (1) already-warm READY/SUSPENDED replica of the model, (2) any GPU,
  (3) most free RAM, with `agent_id` as the deterministic tiebreak. Warm-replica
  beats raw RAM: skipping a cold model load dominates. `RoundRobinScheduler`
  (arm b) is the capability-blind baseline — it applies only the eligibility gate
  (correctness) and cycles fairly over the `agent_id`-sorted set.
- **One allowed piece of state.** Round-robin cannot be stateless, so it carries
  exactly one integer cursor and `reset()`; everything else stays pure. This is
  documented as the deliberate exception so the "arms are pure" contract still
  holds for measurement.
- **`pick_replica`.** Least `inflight`, tiebreak `host:port` lexicographic;
  capability picks the min, round-robin rotates the same cursor.
- **PULL-based dispatch.** `DispatchLoop.tick()` does exactly one durable thing —
  `await queue.requeue_expired()`. Actual unit handout happens when an idle agent
  long-polls: the app calls `select_for_poll(agent, model_ids, policy)` to
  pre-filter the model list, then `queue.lease_next`. `select_agent` is the app's
  tiebreak when several idle agents compete, and the seam the v2 push scheduler
  will drive from inside `tick()`. The loop holds `policy`+`snapshots` for that.
- **Never dies.** `tick()` converts any store exception into
  `DispatchStats(error=...)`; `run_forever()` loops on an injected `sleep` until
  `stop()`, so a flaky sweep can never kill the coordinator's maintenance loop.

## Consequences

- Deterministic policies mean the whole arm comparison is replayable from a fixed
  agent-snapshot table — no fakes-with-clocks needed for policy tests.
- v0.1 leaves `select_agent`'s cross-agent competition to the app layer; the pure
  policy is ready, but `tick()` stays minimal until v2 turns dispatch push-based.
- One shared round-robin cursor couples agent and replica rotation order; fair
  and deterministic given call order, which is all the experiment needs.
