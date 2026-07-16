# ADR 042: Instant user-triggered takedown (reclaim my device)

Status: accepted · Date: 2026-07-16

## Context

Automatic preemption already yields the machine when it detects the user typing
or moving the mouse. That is reactive and it resumes on its own once the machine
looks idle again. A person sitting at their own machine needs a stronger
guarantee: an explicit "this is mine now" that stops all local serving and stays
stopped until they say otherwise, so their own work is never interrupted by
fallow deciding the machine is free.

This is a control action against a running daemon, not a config change. Windows
has no POSIX signals, so the mechanism has to be cross-platform. It must never
be reachable off-host.

## Decision

Reclaim reuses the existing suspend-first path rather than adding a second
preemption mechanism. On reclaim the daemon calls `supervisor.suspend_all()`
first, exactly as automatic preemption does, for instant compute relief; then it
stops the replicas to free RAM and VRAM. The suspend runs on the poll thread and
clears the preemption budget (p99 well under 300ms); stopping waits on process
exit, so it runs off the poll thread and does not hold up the tick.

Reclaim is sticky. It stays down regardless of idle detection until an explicit
release. While reclaimed the poll loop skips automatic preemption entirely, and
the reconcile loop defers, so nothing relaunches. Release clears the state;
normal idle-based serving resumes and the reconcile loop relaunches replicas on
its next assignment tick.

Control channel: a single flag file under the agent state directory. Its
presence means reclaimed, its absence means released. `fallow_agent reclaim`
writes it and `fallow_agent release` removes it; the daemon's existing poll loop
checks it once per tick. A local file is the simplest thing that works on every
platform and is never reachable off-host — no new socket, no new port, nothing
to authenticate. The alternative, a loopback control socket, would add a
listener, a wire format, and an auth story for a signal a one-byte file already
carries.

Availability on the wire: the heartbeat and `AgentSnapshot` gain a
`serving_paused` boolean. A reclaimed agent reports `serving_paused=true`; the
coordinator persists it and the gateway excludes that agent from interactive
candidates. A boolean, not a tri-state availability enum, keeps the change
minimal and orthogonal to the existing `AgentState` (idle/active/draining),
which is the automatic-preemption state and is left untouched.

In-flight requests: a reclaim prioritizes the user over in-flight work.
Requests that have not yet received their first byte re-route through the
existing gateway retry. Requests that are already streaming may be dropped. We
accept that tradeoff deliberately — the whole point is to give the person their
machine back now, not after the current response finishes.

## Cross-language contract (Go port)

`serving_paused` is a new wire field, regenerated into `schemas/Heartbeat.json`
and `schemas/AgentSnapshot.json` in this PR. It defaults to `false`, so older
agents that never send it read as "serving". The Go agent will need the same
field on its heartbeat and the same reclaim/release control-file behaviour: same
filename under the state dir, presence-means-reclaimed, suspend-first then stop.
This PR is Python only; the schema is the shared source of truth.

## Go port

The Go agent now implements the same primitive, no schema change needed:
`serving_paused` was already generated onto its `Heartbeat` and `AgentSnapshot`
from the shared schema. The port keeps the contract identical to the Python side:
same `reclaim.flag` filename beside the state file, presence-means-reclaimed,
suspend-first then stop off the hot path, sticky until release.

- `go-agent/preempt/reclaim.go` holds `ReclaimController` plus the control-file
  helpers (`RequestReclaim`, `RequestRelease`, `ReclaimControlPath`). It reuses
  the existing Go `ProcessSupervisor.SuspendAll` — the same hot-path call
  automatic preemption uses — rather than adding a second preemption mechanism.
  The stop step runs through an injectable `StopRunner` (a goroutine in
  production, a synchronous runner in tests).
- The daemon's poll loop (`go-agent/runtime/loops.go`) calls the reclaim
  controller once per tick before the idle-driven state machine and skips
  automatic preemption while reclaimed, so nothing resumes on detected idleness.
- The heartbeat (`go-agent/runtime/runtime.go`) reports
  `serving_paused = reclaim.IsReclaimed()`.
- `agentctl reclaim` / `agentctl release` write and remove the flag, reading the
  same config so it lands beside the same state file.

## Consequences

- The user gets an instant, sticky guarantee with one local file and one boolean
  on the wire.
- Routing reacts within one heartbeat (~5s) and immediately as stopped replicas
  drop out of the ready set; a reclaimed agent is never a hang, because the
  gateway excludes it and sheds or reroutes.
- A streaming response can be cut off by a reclaim. This is intended.
- Automatic preemption is unchanged; reclaim sits on top of it and reuses its
  suspend call.

## Verification

Unit tests drive the reclaim state machine (idle → reclaimed → released) with a
recording supervisor and an injected clock, asserting suspend-all runs first and
stop follows, that the state is sticky until release, and that the suspend
clears the latency budget. The poll loop test asserts it skips preemption while
reclaimed; the reconcile test asserts it defers. Registry tests assert a
reclaimed heartbeat is excluded from routing and that release restores it. The
CLI test asserts the subcommands toggle the flag file. The integration scenario
runs two agents serving one model: reclaiming one reroutes to the other,
reclaiming both sheds cleanly, and releasing one restores serving.
