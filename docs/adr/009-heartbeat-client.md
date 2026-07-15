# ADR 009: Coordinator client + heartbeat loop (module A5)

Status: accepted · Date: 2026-07-15

## Context

The agent must talk to the coordinator (register, heartbeat, push events,
long-poll work, report results) from an async main, while events originate on
the synchronous preemption poll thread. Per ADR 000, all traffic is
agent-initiated plain HTTP+JSON and `EventSink.emit` must never block the caller.

## Decisions

1. **One typed client, injected transport.** `CoordinatorClient` wraps an
   injected `httpx.AsyncClient`; tests drive it with `httpx.MockTransport`. It is
   a stateful *connection* object holding `agent_id` + bearer `device_token`
   (connection state, not domain data — wire messages stay frozen).
2. **Three-way error taxonomy.** `CoordinatorAuthError` (401/403),
   `CoordinatorTransientError` (transport failure or 5xx), `CoordinatorProtocolError`
   (bad status / malformed body). Callers branch on class, not strings.
3. **Retry only idempotent transport failures.** `heartbeat` and `poll_work`
   retry `httpx.TransportError` with injected sleep + exponential backoff.
   `register` is never retried; 5xx is classified transient but not retried
   in-line (the loop/sink decides).
4. **Non-blocking event sink via a stdlib janus pattern.** `emit` (any thread)
   puts on an unbounded `queue.SimpleQueue` and calls
   `loop.call_soon_threadsafe(wake.set)`; a background asyncio task awaits the
   `Event` and drains. We deliberately avoid `run_coroutine_threadsafe` — no
   per-event `Future` is created, so `emit` is a lock-free enqueue plus one
   thread-safe callback schedule, never a cross-thread round-trip.
5. **JSONL is the durable copy.** Each event is appended to a local
   `events.jsonl` *before* the best-effort coordinator push; the push is dropped
   after `max_push_attempts`. Local disk, not the network, is the source of truth
   for the audit trail.
6. **Un-killable heartbeat loop.** Only an auth rejection stops it (surfaced via
   `on_auth_error`); transient/protocol/unexpected errors are logged and the loop
   keeps beating. `seq` increments every iteration. Reconciling each
   `HeartbeatResponse` is handed to `on_response` (wave-3's job), not done here.
7. **Hardware behind seams.** `SystemProbe` (psutil) and `GpuProbe` (NVML, lazily
   imported, degrades to `()` without an NVIDIA GPU) isolate all platform I/O so
   metric assembly is deterministic in tests.

## Consequences

- Events emitted before `start()` or after `stop()`'s final flush may be lost
  from the push path; the JSONL still captures pre-start events on drain.
- A `SimpleQueue` + wakeup `Event` means at most one spurious wakeup per drain —
  cheap, and no event can sit un-drained (every put is followed by a wakeup that
  drains to empty).
- Single bearer token in memory; rotation on a `HeartbeatResponse.config` change
  is wave-3's concern.
