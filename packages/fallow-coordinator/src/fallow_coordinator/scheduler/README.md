# scheduler — placement policies + dispatch loop (coordinator module C4)

Decides *which idle agent* should receive a batch unit and *which replica*
serves an interactive request. The policies are pure implementations of
`fallow_protocol.interfaces.SchedulerPolicy` — deterministic given their inputs —
so the experiment arms are hot-swappable and unit-testable. The one impure piece,
`DispatchLoop`, is where the injected clock, queue store, and live agent view
meet.

## Public API

Re-exported from `fallow_coordinator.scheduler`:

- `CapabilityScheduler()` — arm (c), the v1 default: capability-aware placement.
- `RoundRobinScheduler()` — arm (b): capability-blind fair rotation; `reset()`.
- `ChurnAwareScheduler(model, est_unit_duration_s=60.0, *, hour_fn)` — arm (c) v2:
  ranks eligible agents by modelled `P(stays idle ≥ est duration)`, v1 order as
  the tiebreak. `pick_replica` is unchanged from v1 (ADR 022).
- `ChurnModel` / `build_churn_model(events, hour_of)` — pure empirical
  idle-survival model built from `events.jsonl` mappings (no I/O in this module).
- `DispatchLoop(queue, snapshots, policy, requeue_interval_s, now, sleep=asyncio.sleep)`
  with `await loop.tick() -> DispatchStats`, `await loop.run_forever()`, `loop.stop()`.
- `DispatchStats(at, requeued=0, error=None)` — frozen per-tick outcome.
- `select_for_poll(agent, model_ids, policy) -> tuple[str, ...]` — per-long-poll
  pre-filter for `queue.lease_next`.

```python
policy = CapabilityScheduler()
agent_id = policy.select_agent("qwen2.5-7b", needs_gpu=True, agents=snapshots)
replica = policy.pick_replica("qwen2.5-7b", endpoints)

loop = DispatchLoop(queue, get_snapshots, policy, requeue_interval_s=10.0, now=clock)
stats = await loop.tick()          # sweeps expired leases, never raises

leasable = select_for_poll(agent, agent.assigned_models, policy)
lease = await queue.lease_next(agent.agent_id, leasable)
```

## Behaviour / invariants

- **Purity.** `select_agent` / `pick_replica` perform no I/O, read no clock, and
  use no randomness. `RoundRobinScheduler` is the sole exception: it carries one
  integer cursor (round-robin is impossible without state); `reset()` restores
  the deterministic start.
- **Eligibility gate (both arms).** An agent is eligible iff it is `IDLE`, not
  `suspect`, and — when `needs_gpu` — has a registered GPU. Ineligible agents can
  never be selected; this is correctness, not preference.
- **Capability ranking (arm c).** Among eligible agents, rank by: (1) already
  holds a warm READY/SUSPENDED replica of the model, (2) has any GPU, (3) most
  `mem_available_mb`; final tiebreak `agent_id`. A warm-replica agent beats a
  higher-RAM agent — avoiding a cold model load dominates.
- **Round-robin (arm b).** Ignores every capability signal; cycles fairly over
  the `agent_id`-sorted eligible set. One cursor advances on every handout
  (agents and replicas share it).
- **`pick_replica`.** Narrows to replicas serving the model, then least
  `inflight`, tiebreak `host:port` lexicographic. `CapabilityScheduler` picks the
  minimum; `RoundRobinScheduler` rotates.
- **`select_for_poll`.** Returns `()` for an unfit agent (not IDLE, or suspect),
  else the model_ids unchanged. `policy` is threaded for signature stability and
  the future v2 per-model prune.
- **`DispatchLoop.tick`.** v0.1 does exactly one durable thing — `await
  queue.requeue_expired()` — because handout is PULL-based. It never raises: a
  store exception becomes `DispatchStats(error=...)`. `run_forever` loops on the
  injected `sleep` until `stop()`.

## Files

- `policies.py` — `CapabilityScheduler`, `RoundRobinScheduler`.
- `v2.py` — `ChurnAwareScheduler` (arm c v2; see ADR 022).
- `churn_model.py` — `ChurnModel`, `build_churn_model` (empirical idle-survival).
- `_eligibility.py` — pure eligibility/ranking predicates shared by all arms.
- `poll.py` — `select_for_poll`.
- `dispatch.py` — `DispatchLoop`, `DispatchStats`.
