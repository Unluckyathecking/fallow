# ADR 045: Go agent daemon (`agentctl run`)

**Status:** accepted

**Date:** 2026-07-16

## Context

E4.2 (ADR 037) and E4.3 (ADR 038) ported the agent's building blocks to Go: the
coordinator client (`heartbeat`), the preemption controller (`preempt`), the
process supervisor (`supervisor`), the model cache (`modelcache`), idle detection
(`idle`), and identity persistence (`state`). What was missing was the top-level
loop that composes them into a process. `agentctl` had only the one-shot
conformance subcommands (register, heartbeat, poll, upload, complete) that the
E4.4 parity harness threads together by hand, so the Go agent could not run as a
daemon. That gap blocked the `--go-binary` deploy install (deferred out of #45)
and meant the parity harness only ever exercised individual wire calls, never a
running agent.

This ADR covers the composition — `agentctl run --config x.toml` — not any new
protocol. The Python `AgentRuntime` / `AgentAssembly` / `AgentServices` trio is
the specification for structure and shutdown order.

## Decision

### A `runtime` package that composes, and a `config` package that reads the shared file

`runtime.Runtime.Run(ctx)` mirrors the Python runtime: resolve identity (register
or resume), wire the components, start the loops, block until the context is
cancelled (SIGINT/SIGTERM) or a fatal auth rejection fires, then tear down. The
composition is flatter than the Python assembly — Go goroutines coordinated by a
context and a `WaitGroup` rather than a services object of start/stop methods —
but the responsibilities map one to one.

`config.Load` reads the same agent TOML the Python agent reads, with the same
environment overrides (env wins) and the same security-critical rule: `bind_host`
must never be `0.0.0.0`, because llama-server has no auth (ADR 003). It decodes
only the keys the daemon composes and ignores the richer Python-only sections
(`whisper`, `bench`, `results_dir`), so one file serves either agent.

### The three loops

- **Heartbeat loop.** Every `heartbeat_interval_s` it assembles a `Heartbeat`
  from the controller's state, the supervisor's replica statuses, and an idle
  sample, and sends it. It is un-killable by anything short of an auth rejection:
  transient and protocol failures are logged and the loop keeps beating (ADR 009);
  an `*AuthError` is surfaced as fatal and stops the daemon.
- **Preempt poll loop.** Every `poll_interval_ms` it samples the idle detector and
  advances the controller one tick — the already-tested suspend-first / hysteresis
  / GPU-eviction machine, driven, not reimplemented.
- **Work poll loop.** Gated on a wired `Runner` (see the deferral note below): with
  one, while IDLE it long-polls for a lease and hands it over, and while the user is
  active it sleeps and re-checks so the machine is never touched; with none, it does
  not poll at all.

### Shutdown order (ADR 015)

On a signal or a fatal auth error: drain the controller (stop accepting work, emit
`agent_stopping`), cancel the loops and join them, send one final DRAINING
heartbeat on a fresh short-lived context, stop every replica, then flush the event
sink. A small buffered sink forwards preemption events to `PushEvent` off the hot
path, so `Emit` never blocks the poll thread.

### Seams for a deterministic wiring test

The coordinator client, supervisor, idle detector, runner, clocks, and periodic
ticker are all injectable, defaulting to the production implementations. The unit
test drives the whole loop against a recording fake coordinator, a recording fake
supervisor, a fake detector, and hand-fired tickers: it asserts enrollment
persists a 0600 state file, the first heartbeat is IDLE and carries the
supervisor's replicas, the work loop polls, a fresh-input tick suspends, and a
cancel produces a DRAINING final heartbeat followed by `StopAll` — plus the
resume-from-disk and fatal-auth paths.

### Idle sampling is guarded

Both loops sample idle through one helper that rejects a non-finite reading (some
OS idle APIs return NaN off a GUI session) and an unsupported detector. The
heartbeat then reports a large "away" value rather than shipping a bogus number,
and the preempt loop skips the tick rather than driving the state machine on
garbage. This was a real bug: an unguarded NaN `user_idle_s` is rejected by the
coordinator at enrollment.

### Deliberately deferred

- **Model reconcile.** The daemon supervises replicas and stops them cleanly, but
  it does not yet download assigned models and start replicas from the heartbeat's
  `desired_models`. That needs a Go manifest fetcher and port allocator — closer
  to new protocol client code than composition — and lands separately.
- **A Go work runner.** There is no Go batch worker yet, so no runner is wired and
  the work loop is *gated on a runner*: with none present it does not poll at all.
  This is deliberate and load-bearing. Leasing a unit increments its attempt (the
  coordinator's `CLAIM_UNIT`), and a unit leased then abandoned four times is
  dead-lettered — so a daemon that leased work it could not execute would destroy
  real jobs, not harmlessly requeue them. The gate makes that impossible until a
  runner lands, and it holds regardless of when model-reconcile ships: even a
  daemon with READY replicas will not lease without a runner. Batch execution stays
  on the one-shot parity path.
- **Host metrics.** Enrollment reports real CPU-core count and conservative
  placeholders for RAM/disk (under-reporting only ever excludes this agent from
  models it might fit, never the reverse) until a Go probe lands.

### Parity harness

The E4.4 suite gains a daemon flavour: `test_goagent_daemon_*` launches
`agentctl run` as a real subprocess against the in-process coordinator and asserts
it enrolls, heartbeats itself visible, and shuts down cleanly on a signal. The
existing scenarios keep shelling the one-shot subcommands, and the module
documents which path each covers. The one-shot subcommands are untouched, so the
rest of the harness is unaffected.

## Consequences

- The Go module gains one dependency, `github.com/BurntSushi/toml`, to read the
  shared config; the standard library has no TOML decoder and a hand-rolled subset
  parser would be more code and less trustworthy.
- The daemon is enough to run as a persistent agent that enrolls, stays visible,
  yields to the user, and stops cleanly — which unblocks the `--go-binary` deploy
  install — but it does not yet serve models end to end; reconcile and a work
  runner are the remaining E4 work before it reaches Python parity for batch.
- POSIX shutdown is graceful (SIGINT runs the full teardown); on Windows a
  non-attached child has no catchable console signal, so the parity test kills it
  and asserts only the visibility, matching how the one-shot suite already guards
  Windows-specific behaviour.
