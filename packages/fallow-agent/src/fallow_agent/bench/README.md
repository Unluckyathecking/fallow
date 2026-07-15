# bench — module A7

Agent-side bench hooks. Let the Wave-4 churn injector (module B2) simulate a
user returning to a headless benchmark machine — **without** real keyboard or
mouse input — so the 3-arm scheduling experiment can drive reproducible churn.

## Purpose

The signature Fallow behaviour is yielding the machine the instant its owner
returns. To measure that (time-to-yield, recovery time, %-served-on-prem) across
scheduling arms, the experiment must *cause* returns on demand. There is no human
at the benchmark box, so this module fabricates the one signal the preemption
poll thread reacts to: a drop in "seconds since last input".

This module does **not** decide when to churn (that is B2) and does **not**
implement the yield state machine (that is preempt / A2). It only provides the
injection seam and a tiny control surface for it.

## Public API

Re-exported from `fallow_agent.bench`:

| Symbol | Purpose |
| --- | --- |
| `BenchIdleDetector(inner, monotonic=...)` | Wraps any `IdleDetector`; after `simulate_input()` reports `0` rising from an injected monotonic clock until the inner detector reports a smaller value. |
| `BenchListener(host, port, detector, state_source)` | Stdlib asyncio HTTP listener serving the two B2 routes. |

Headless Linux experiment hosts may set both `[bench] enabled = true` and
`force_idle = true`. Settings validation and the idle factory reject forced idle unless
bench mode is enabled. Startup logs one warning when the constant detector is active.
Never use this setting on a machine used by a person.

## HTTP contract (fixed for B2)

Bound to the agent's `bind_host` (never `0.0.0.0`) on `[bench] port` (default
`9411`):

| Route | Response |
| --- | --- |
| `POST /simulate_input` | `204 No Content` — calls `BenchIdleDetector.simulate_input()`. |
| `GET /state` | `200` JSON `{"state": "idle\|active\|draining", "idle_s": float}` from the Preemptor + detector. |
| anything else | `404 Not Found` (unparseable request line → `400`). |

## Invariants

- **Real input wins.** Once the inner (OS) detector reports a value below the
  synthetic one, a genuine event happened more recently — the injection is
  cleared and the real reading passes through. Injection can never mask a real
  return.
- **Thread-safe.** The poll thread reads `seconds_since_input()` while a bench
  thread calls `simulate_input()`; injection state is guarded by a lock.
- **No framework.** The agent layer may not import `fastapi`/`aiosqlite`
  (import-linter); the listener is a hand-rolled `asyncio.start_server` with
  minimal HTTP/1.1 parsing.
- **No auth, off by default.** The surface exists only when `[bench] enabled =
  true`, has no authentication, and is meant for loopback/tailnet-only
  benchmark use; the `bind_host` `0.0.0.0` guard still applies.
- **Injected clock.** `BenchIdleDetector` takes a `monotonic` callable so tests
  are replay-deterministic; nothing here reads a wall clock.

## Wiring

When `[bench] enabled = true`, `AgentAssembly` wraps the OS idle detector in a
`BenchIdleDetector` (so heartbeats, the poll thread, and the final beat all see
the injected value) and constructs a `BenchListener`; `AgentServices` owns its
start/stop lifecycle.

With `force_idle = true`, the wrapper receives a finite constant-idle detector instead of
the platform detector. A simulated return still resets the wrapper to zero, so churn uses
the same preemption path as every other bench run.

## Testing

`FakeIdleDetector` + an injected clock cover passthrough, injection, and
real-input precedence. The listener is exercised over a real loopback socket on
an ephemeral port with raw HTTP — no framework, no external network.
