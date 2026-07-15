# ADR 020: Bench churn injector (module B2)

Status: accepted · Date: 2026-07-15

## Context

The Wave-4 scheduling study (arms: single-machine, round-robin, churn-aware v2)
must be run against a fleet whose users come and go the same way every time, or
the arms are not comparable. The headline metrics — time-to-yield, recovery
time, %-served-on-prem — all depend on *when* users return to their machines. We
need a component that manufactures that churn reproducibly, records exactly what
it did, and can also replay hand-authored adversarial scenarios.

Bench-mode agents (module A7) expose a fixed HTTP contract:
`POST /simulate_input → 204` and `GET /state → {"state","idle_s"}`. B2 drives
that contract; it must not import `fallow_agent` (bench layer rule: only
`fallow_protocol` + third party).

## Decision

- **Seeded generator, one RNG.** `build_schedule(section)` draws every value
  from a single `numpy.random.default_rng(seed)`. Each agent is an independent
  idle→active renewal process: idle gaps and active-session durations are
  lognormal (`idle_*` / `active_*` mu/sigma). A session emits **user-return
  taps** every `tap_interval_s` (< the agent's idle threshold) so the machine
  stays active for the whole session. Same seed ⇒ byte-identical schedule
  (offsets rounded to 6 dp; stable `(t, agent, kind)` sort).
- **user_return dominates; kill/net_drop are opt-in.** `agent_kill` and
  `net_drop` are low-rate Poisson extras, **off by default** (`rate = 0`). When
  enabled they are drawn from the same RNG *after* all returns, preserving
  determinism.
- **Scripted override.** `ChurnSection.scripted`, when present, is replayed
  verbatim and the generator is bypassed — this is how hand-authored scenarios
  enter. `load_churn_section` reads a standalone YAML doc or an experiment config
  that embeds the section under a `churn:` key.
- **Injector owns no time.** `ChurnInjector` takes an injected `clock`
  (`Callable[[], float]`) and async `sleep`; it waits until each event's
  monotonic offset, executes, and records. `time.monotonic`/`asyncio.sleep` are
  wired only in `__main__`; tests inject a fake clock, making replays
  deterministic and fast (no real sleeping).
- **Everything is recorded; nothing aborts.** Every executed event becomes a
  `ChurnRecord {t_scheduled, t_executed, agent, kind, ok, detail, flip_ms}`
  appended to `churn.jsonl` via a single-writer async `ChurnLog`. A failing
  endpoint, a missing command template, or a raising runner is logged with
  `ok=false` and the run continues.
- **Flip-latency verification.** After a `user_return` the injector optionally
  polls `GET /state` (bounded by `max_wait_s`) until `state == active`, recording
  `flip_ms` — the end-to-end injected-input → yield latency, a headline metric.
- **Commands are config, never code.** `agent_kill`/`net_drop` run a per-kind
  shell template from config, rendered with `{name}/{host}/{bench_port}`, through
  an injected `Runner`. The subprocess `Runner` (`run_shell`) lives in `runner.py`
  and is imported only by `__main__`; tests inject a recorder.

## Consequences

- Experiment arms see identical churn for a given seed, so cross-arm deltas are
  attributable to the scheduler, not to RNG.
- A session is modelled as repeated sub-threshold taps rather than a single
  return; this keeps a machine active across a long session but means "one
  return" in the data is a tap-train, not a single event. Downstream analysis
  (B3) should group by `params.session` when counting distinct returns.
- kill/net_drop actually mutate the fleet (SIGKILL, network) via shell commands;
  they are off by default and gated entirely on operator-supplied templates.

## Open questions

- **B1 seam.** `ChurnSection` is B2's own dataclass so B2 is buildable before B1
  lands. B1's `ExperimentConfig` YAML is expected to embed it under `churn:`
  (`parse_churn_section` already unwraps that key). If B1 chooses a different key
  or a nested layout, adjust `CHURN_SECTION_KEY` / `parse_churn_section` — the
  models are the stable contract.
- **A7 payload shape.** We rely on `GET /state` returning a JSON object with a
  string `state` field whose active value equals `AgentState.ACTIVE` (`"active"`).
  If A7 ships a richer payload, only `verify._is_active` needs updating.
- **Energy / recovery metrics** are computed by the analysis module (B3) from
  `churn.jsonl` + the coordinator's `events.jsonl`; B2 only emits the raw record.
