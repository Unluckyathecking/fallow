# ADR 037: Go core agent

**Status:** accepted

**Date:** 2026-07-15

## Context

Managed deployments need a single native agent binary, while the coordinator
and wire contract remain in Python. The existing Python agent already defines
the enrollment, heartbeat, event, work-poll, idle, and preemption behavior. A
Go port that changes those rules would split the fleet into two protocols.

Idle detection also differs by operating system. Windows exposes a 32-bit tick
counter that wraps after about 49.7 days. macOS exposes system idle time through
Core Graphics. Linux desktops may use X11, Wayland, or logind, with no common
source that is correct for every session.

## Decision

The Go coordinator client uses the generated types from ADR 036 and the same
HTTP paths as the Python client. Registration is sent once. Heartbeats and work
polls retry transport failures with exponential backoff, while HTTP responses
are classified immediately. Status 401 or 403 is an authentication failure,
5xx is transient, and any other unexpected status is a protocol error. A work
poll returns no lease on 204. Event and result writes accept 200, 201, 202, or
204.

Enrollment state is JSON written through a temporary file in the destination
directory, followed by an atomic replacement. Unix permissions are `0600`.
Windows replaces the file with `MoveFileEx`; the user's profile directory ACL
provides the effective protection because POSIX mode bits are advisory there.

Windows idle detection calls `GetLastInputInfo` and subtracts unsigned 32-bit
ticks, which preserves the correct elapsed time across rollover. macOS calls
`CGEventSourceSecondsSinceLastEventType` through cgo and ApplicationServices.
Linux returns an unsupported error until a session-aware implementation covers
its separate display systems.

The preemption controller keeps the Python state transitions. Fresh input is
either an idle duration shorter than the poll period or a drop from the prior
reading. The first user-return side effect is `SuspendAll`. Work resumes only
after the full idle threshold. Once the VRAM delay expires, the controller
stops suspended GPU replicas and leaves CPU replicas alone. Draining is
terminal.

The poll loop runs in its own goroutine. It catches detector errors and panics
inside each iteration so one bad reading does not disable future preemption.
The event sink queues calls from that path, writes a local JSONL copy, and sends
events in the background.

## Consequences

- Python and Go agents use the same coordinator deployment and API keys.
- E4.3's supervisor satisfies the preemption interface through `SuspendAll`,
  `ResumeAll`, `Stop`, and `Statuses`.
- macOS builds require a working cgo toolchain. A build with cgo disabled
  returns an explicit runtime error from idle detection.
- Linux cannot run the ordinary idle-sensitive daemon yet. It fails instead of
  risking work during an active desktop session.
- CI runs Go tests and vet on Linux, macOS, and Windows so every platform file
  is compiled on its target operating system.
