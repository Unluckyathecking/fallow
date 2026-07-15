# idle

Answers one question — "how many seconds since the user last touched this
machine, right now?" — cheaply, without blocking or spawning a process. Ports
`fallow_agent.idle`. Polling and the yield/resume state machine live in
`preempt`, not here.

## Detector selection

`CreateDetector(benchEnabled, forceIdle)` returns the `Detector` for the current
OS. The concrete implementation is chosen at **compile time** by build tags:

| GOOS | Constraint | Source | Behavior |
| --- | --- | --- | --- |
| windows | `windows` | `detector_windows.go` | `GetLastInputInfo` + `GetTickCount` via `golang.org/x/sys/windows` |
| darwin | `darwin && cgo` | `detector_darwin.go` | `CGEventSourceSecondsSinceLastEventType` (cgo, CoreGraphics) |
| darwin | `darwin && !cgo` | `detector_darwin_stub.go` | unsupported (no Quartz without cgo) |
| linux | `linux` | `detector_linux.go` | honest stub: `ErrUnsupported` |
| other | `!windows && !darwin && !linux` | `detector_other.go` | unsupported |

`go build ./...` therefore succeeds on macOS (dev) and ubuntu (CI) alike — each
build compiles exactly one `newPlatformDetector`.

`forceIdle` requires `benchEnabled` and yields a `ConstantDetector` (largest
finite idle duration) so an ordinary agent can never accidentally select it.

## Windows wraparound

`GetTickCount()` and `LASTINPUTINFO.dwTime` are unsigned 32-bit millisecond
counters that wrap to zero every 2³² ms (~49.7 days). The elapsed-time
arithmetic lives in `ticks.go` (build-tag-free) as `uint32` subtraction, which
wraps modulo 2³² automatically — so it is compiled and unit-tested on **every**
platform, not just Windows. `WindowsDetector` takes an injectable `TicksReader`;
only the default reader (the real Win32 seam) is Windows-only.

## Linux is deliberately a stub

A correct Linux detector must span X11 (XScreenSaver), Wayland (per-compositor
idle-notify), and headless/logind, with no single cheap API covering all three.
Rather than ship a silently-wrong detector, v0.1 reports `ErrUnsupported` so
callers fail loudly.

## Testing

`FakeDetector` is a settable, thread-safe `Detector` for unit tests and the
bench churn injector (invariant: reported idle ≥ 0). The wraparound arithmetic,
the reader-injected `WindowsDetector`, the fake, and the factory are all tested
in `idle_test.go` on the host platform.
