# ADR 001: Idle detection (module A1)

Status: accepted · Date: 2026-07-15

## Context

The preemption state machine needs "seconds since the user last touched this
machine" at ~10 Hz on the hot path. Per ADR 000, the agent must yield within
300ms (p99) of user input, so the reading must cost microseconds and must never
spawn a process, block, or hit the network. Each OS exposes a different native
API, and the module must import on every platform even where its native
dependency is absent (CI and dev laptops are macOS/Linux; targets are Windows).

## Decision

One thin `IdleDetector` implementation per OS behind a `create_idle_detector()`
factory that dispatches on `sys.platform`:

- **Windows** — `GetLastInputInfo` + `GetTickCount` via `ctypes`. Both values
  are unsigned 32-bit DWORDs, so elapsed time is computed modulo `2**32` to
  survive the ~49.7-day tick rollover. All `windll` access lives inside a
  `sys.platform == "win32"` branch so the module imports elsewhere.
- **macOS** — Quartz `CGEventSourceSecondsSinceLastEventType`, imported lazily
  by name through `importlib` so pyobjc is only required on macOS.
- **Linux** — an honest stub that raises `NotImplementedError` (X11/Wayland/
  logind have no single cheap API; out of scope for v0.1).

Each detector isolates its OS call behind one tiny, injectable `reader`
function, making the wraparound arithmetic and dispatch fully unit-testable with
no OS, GPU, network, or subprocess. A thread-safe `FakeIdleDetector` serves
tests and the bench churn injector.

## Consequences

- Wraparound correctness is verified directly against `2**32`-boundary inputs.
- The agent must run in the user's interactive session on Windows; a Session 0
  service reads nothing and would look permanently idle (documented in README).
- Linux hosts fail loudly instead of silently reporting wrong idle time.
- No polling or threading lives here; that is module A2 (preemption), keeping
  this module a pure, side-effect-free reading.
