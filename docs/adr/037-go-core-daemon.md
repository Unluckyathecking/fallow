# ADR 037: Go core daemon (heartbeat, idle, preempt, state)

**Status:** accepted

**Date:** 2026-07-15

## Context

E4.1 established the Go `protocol` package: wire types generated from the JSON
Schemas (ADR 036), with a conformance fixture suite. E4.2 ports the *behavioral*
core of the managed agent to Go — the pieces that talk to the coordinator, read
the machine, and decide when to yield — while the coordinator stays in Python.
The Python source under `packages/fallow-agent/` is the specification; the Go
port must match its observable semantics, not merely its shapes.

Four modules are in scope: the coordinator HTTP client (`heartbeat`), idle
detection (`idle`), the preemption state machine (`preempt`), and identity/state
persistence (`state`).

## Decision

### HTTP client semantics parity

`heartbeat.Client` mirrors `CoordinatorClient` call-for-call and, crucially,
status-code-for-status-code:

- `Register` accepts 200/201, sends **no** bearer, and is **never** retried (a
  duplicate enrollment is not idempotent).
- `Heartbeat` / `PollWork` are idempotent and retry **only transport errors**
  (never a status code) with injected exponential backoff; `PollWork` treats 204
  as "no work" (nil lease).
- `PushEvent` / `CompleteUnit` are fire-and-forget and accept 200/201/202/204.
- 401/403 → `*AuthError`, ≥500 → `*TransientError`, anything else (or a
  malformed body) → `*ProtocolError`.

All I/O is behind a `Doer` interface so tests use `httptest` and a
failure-injecting `RoundTripper`. The Python client's behavioral cases are
ported against that seam.

### Build-tag strategy for idle

The concrete detector is selected at **compile time**, so `go build ./...`
succeeds identically on macOS (dev) and ubuntu (CI). Each platform file defines
one `newPlatformDetector`: `windows` binds `GetLastInputInfo`/`GetTickCount`
lazily through `golang.org/x/sys/windows`; `darwin && cgo` calls
`CGEventSourceSecondsSinceLastEventType` via CoreGraphics; `darwin && !cgo` and
`linux` are honest `ErrUnsupported` stubs (a correct Linux detector would need
X11 + Wayland + logind sources and is out of scope for v0.1). The Windows
wraparound arithmetic and the reader-injected `WindowsDetector` live in a
build-tag-free file so they compile and are unit-tested on every platform;
`GetTickCount` is an unsigned 32-bit ms counter that wraps ~49.7 days, handled
by `uint32` modular subtraction.

### Preempt state machine port

`preempt.Controller` is a 1:1 port of `PreemptController`, including the single
hard rule: on user return, `SuspendAll` is the first side effect (suspend →
measure `yield_ms` → emit `user_returned`, in that order). Hysteresis is
intrinsic to `idle_s` (seconds since last input), GPU-only VRAM eviction fires
once after `vram_evict_after_s`, and `Drain()` is terminal and idempotent. The
Python `test_preempt_controller.py` cases are ported case-for-case; the
semantics were not "improved."

### The omitempty / interop proof

Nil Go slices marshal to JSON `null`. The coordinator's frozen Pydantic models
(`extra=forbid`; `gpus`/`replicas`/`lease_ids` are tuple defaults, i.e. **not**
`Optional`) reject `null` with 422. The generator (ADR 036) already tags every
non-required array/object field with `,omitempty`; we **verified** that this
covers every collection field a Go daemon can emit — `Heartbeat.gpus`,
`.replicas`, `.lease_ids`, `DeviceCaps.gpus`, `AgentEvent.detail` — so no
generator change was needed. Fields that the receiving Python model *does* type
as `Optional` (`load_avg`, `temp_cpu_c`, `WorkResult.error`/`metrics`/
`result_ref`) remain pointers that correctly send `null`.

Rather than trust that by inspection, `go-agent/interop` boots the **real**
Python coordinator as a subprocess on `127.0.0.1` (via `uv run --no-sync python
-m fallow_coordinator serve`), then POSTs a Go-marshaled `RegisterRequest` (empty
`caps.gpus`) and `Heartbeat` (empty `gpus`, `replicas`, and `lease_ids`) and
asserts 2xx acceptance — the true merge gate. It also asserts, before sending,
that those keys are absent from the marshaled JSON. The test skips gracefully
(`t.Skip`) when `uv` or the coordinator environment is unavailable, so the
path-gated ubuntu `go.yml` job stays green; it runs and passes on a synced dev
machine.

## Consequences

- The Go agent gains one external dependency, `golang.org/x/sys` (Windows idle
  seam only; compiled out on other platforms).
- The interop test is the authoritative guard against the `null`-vs-`omitempty`
  regression; a future schema change that marks a daemon-emitted collection as
  `required` would drop its `omitempty` and this test would fail loudly.
- The daemon does not yet wire these modules into a running process (the poll
  loop, heartbeat loop, event sink, and supervisor are later E4 work); this ADR
  covers the ported building blocks and their contracts.
- Idle detection reports `ErrUnsupported` on Linux and on cgo-less macOS builds,
  matching the Python stubs; production agents run on Windows/macOS GUI sessions.
