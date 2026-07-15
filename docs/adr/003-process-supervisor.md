# ADR 003: Inference process supervisor (module A3)

Status: accepted · Date: 2026-07-15

## Context

The agent must own every fallow-launched inference child (llama-server,
faster-whisper), gate them to readiness, and — per ADR 000 §3 — suspend all of
them within 300ms (p99) of user input. The suspend path is driven synchronously
from the poll thread and must not block, spawn, or touch the network. Readiness
and crash detection, by contrast, are inherently slow (an HTTP `/health` poll)
and must not stall the hot path.

## Decision

Implement `ProcessSupervisor` as `ChildProcessSupervisor`:

- **Injected seams.** Command construction (`CommandFactory`), the readiness
  probe (`HealthCheck`), the process spawner (`spawn`), and the clock
  (`monotonic`) are all injected, so lifecycle behaviour is deterministic under
  test with real tiny child processes and no HTTP/GPU. The real llama-server
  argv builder (`llama_server_command`) lives behind `CommandFactory`.
- **One health thread per child** handles the slow work: it polls `GET /health`
  until 200 → `READY` (else kill at `startup_timeout_s`), then keeps polling
  `popen.poll()` to detect unexpected death → `STOPPED` (the reap loop).
- **Single lock, never held across blocking calls.** `suspend_all`/`resume_all`
  take the lock only to snapshot live children and to commit states; the psutil
  `suspend()`/`resume()` syscalls run in between. This keeps preemption in the
  sub-millisecond range and makes lock ordering trivially safe.
- **Security:** `SupervisorConfig.bind_host` defaults to loopback and rejects
  `0.0.0.0`, because llama-server is unauthenticated; production binds to the
  tailnet IP (ADR 000 §6).
- **Caller owns ports;** the supervisor uses the port it is given. `statuses()`
  returns a cached tuple with `inflight=0` until the gateway lands.

## Consequences

- Vanished processes are pruned to `STOPPED` on the next suspend/resume or by
  the reap loop; suspending a dead child never raises.
- `resume_all` restores pre-suspend state, so a mid-load suspension does not
  falsely report `READY`.
- Health/reap latency is bounded by `health_poll_interval_s` (default 500ms);
  crash detection is therefore eventual, not instant — acceptable for v0.1.
- `inflight` is a known stub; wiring it requires the gateway (later wave).
