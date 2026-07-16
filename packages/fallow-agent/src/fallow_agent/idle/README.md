# idle — module A1

Idle detection for the Fallow agent. Implements
`fallow_protocol.interfaces.IdleDetector` per OS, plus a factory and a fake.

## Purpose

Answer one question — **"how many seconds since the user last touched this
machine, right now?"** — cheaply enough to call at ~10 Hz from the preemption
poll thread. Every call is microseconds, spawns no process, and never blocks or
touches the network.

This module does **not** poll, thread, or decide when to yield. That is the
preemption module (A2). This module only reports a single instantaneous number.

## Public API

Re-exported from `fallow_agent.idle`:

| Symbol | Purpose |
| --- | --- |
| `create_idle_detector() -> IdleDetector` | Factory; dispatches on `sys.platform`. Production entry point. |
| `WindowsIdleDetector` | Win32 `GetLastInputInfo` + `GetTickCount`. |
| `DarwinIdleDetector` | CoreGraphics `CGEventSourceSecondsSinceLastEventType` (ctypes). |
| `LinuxIdleDetector` | X11 `XScreenSaverQueryInfo` (ctypes); headless/no-libXss hosts report always-idle. |
| `ConstantIdleDetector` | Finite idle reading available only through the guarded bench path. |
| `FakeIdleDetector` | Settable, thread-safe fake for tests and the bench churn injector. |

Each OS detector accepts an optional `reader` callable — the single I/O seam —
so tests inject deterministic readings instead of calling the OS.

## Invariants

- `seconds_since_input()` is O(1), microseconds, non-blocking, spawns nothing.
- Reported value is always `>= 0`.
- **Windows tick math wraps.** `GetTickCount()` and `LASTINPUTINFO.dwTime` are
  unsigned 32-bit DWORDs that roll over every ~49.7 days; elapsed time is taken
  modulo `2**32`, so readings stay correct across the rollover.
- **Windows session requirement.** The agent must run in the user's interactive
  console session. A Session 0 service reads nothing useful from
  `GetLastInputInfo` and would report the machine as permanently idle.
- **macOS import safety.** The CoreGraphics function is bound through ctypes,
  loaded lazily and only inside the darwin branch, so this package imports
  cleanly on non-macOS hosts with no pyobjc dependency (issue #34).
- **Linux covers three cases** (ADR 044). X11 desktops read
  `XScreenSaverQueryInfo` via libXss (ctypes, no new dependency); headless
  servers/VMs (no `DISPLAY`) and hosts without a usable libXss report
  always-idle, logged once so a real desktop never degrades silently.
- **Constant idle is benchmark-only.** The factory refuses it unless bench mode is enabled.

## Testing

`FakeIdleDetector` and the injectable `reader` seams mean no test touches the
OS, a GPU, the network, or a real process. Wraparound arithmetic is unit-tested
directly against `2**32`-boundary inputs.
