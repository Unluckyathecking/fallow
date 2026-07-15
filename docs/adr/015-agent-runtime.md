# ADR 015: Agent runtime — the composition root (module I2)

Status: accepted · Date: 2026-07-15

## Context

Every agent module was built in isolation behind an ABC in
`fallow_protocol.interfaces` (idle A1, preempt A2, supervisor A3, modelcache A4,
heartbeat A5, workers A6). This module is the one place they are wired into a
running daemon: resolve identity, open the coordinator uplink, keep local
replicas reconciled to the coordinator's wishes, run batch work while the
machine is idle, and shut down without a trace when the user returns or the
process is signalled. It is the top of the agent layer DAG
(`main → heartbeat|workers → idle|preempt|supervisor|modelcache`) and imports
only the public APIs of those modules.

## Decisions

1. **Composition root, not logic.** `AgentAssembly.build` is the only place the
   concrete modules are named together. All policy already lives in the modules;
   this wires them. Every external dependency (OS idle detector, HTTP client,
   process supervisor, host telemetry, clocks, sleeps) enters through one frozen
   `RuntimeSeams` bundle, so the whole assembly runs in tests with no network, no
   llama-server, and no GPU.
2. **Settings: frozen, TOML + env, env wins.** `AgentSettings` is an immutable
   pydantic model loaded from a TOML file with `FALLOW_*` environment overrides.
   The security-critical validation mirrors the supervisor (ADR 003): `bind_host`
   must never be `0.0.0.0` — llama-server has no auth, so binding to all
   interfaces would expose an open inference endpoint. Rejected at settings load,
   before the supervisor is ever constructed.
3. **Enroll exactly once; persist 0600.** First run registers with the
   enrollment token and writes `{agent_id, device_token}` to the state file
   created with `os.open(…, 0o600)` and `os.replace` (atomic, owner-only — it is
   a bearer secret). Every later run loads it and skips registration. The initial
   `AgentConfig` comes from the registration response; on a loaded-from-disk run
   it defaults and is refreshed by the first heartbeat response.
4. **Reconcile loop, IDLE-gated, own cadence.** A ~5s async loop diffs
   `HeartbeatResponse.desired_models` (threaded in via `on_response`) against the
   supervisor's live replicas. It acts only while the preemptor is IDLE: it
   fetches each missing model's manifest, `ModelStore.ensure`s the blob, allocates
   a port, and `start_replica`s it; it stops running-but-undesired replicas.
   Replicas killed by VRAM escalation reappear as STOPPED and restart naturally
   here once IDLE. Every per-model action is wrapped — a bad manifest, download,
   or spawn is logged and skipped, never fatal.
5. **Manifest fetch is a tiny typed GET, not an A5 change.** A4 pulls the blob;
   the runtime also needs the manifest (`GET /v1/models/{id}/manifest`, ADR 007).
   Rather than widen the A5 client (ADR 009 keeps it minimal), `ManifestFetcher`
   is a small typed call reusing the same base URL and device token.
6. **Work loop, IDLE-gated, per-unit slack timeout.** While IDLE it long-polls
   for a lease, runs it through the A6 `WorkUnitRunner`, and completes it. While
   ACTIVE it does no work at all — it sleeps cheaply and re-checks. If the user
   returns mid-unit the local replica is suspended and the worker's HTTP call to
   it stalls; the loop caps each unit with an `asyncio.timeout` sized to the
   lease's remaining slack and, on timeout, reports nothing — lease expiry
   requeues the unit elsewhere. Double-running is harmless because units are
   content-addressed and completions dedup (ADR 005). This is a deliberate,
   honestly-documented trade: one interrupted unit is re-run, never lost, and the
   returning user is never made to wait on fallow work.
7. **Deterministic port allocator.** A tiny pure class hands out the lowest free
   port in the configured range and reuses released ports. The supervisor never
   allocates ports (ADR 003); the runtime does, and the reconcile loop releases a
   model's port when it stops (or before a restart) so escalation-killed replicas
   never leak ports.
8. **Explicit lifecycle with a pinned shutdown order.** `AgentServices` owns
   startup (event sink → poll thread → heartbeat → reconcile → work) and graceful
   shutdown: drain the preemptor (emit `AGENT_STOPPING`), stop the work +
   reconcile loops, stop the periodic heartbeat, send one final DRAINING
   heartbeat, stop the poll thread, `stop_all` replicas, then flush the event
   sink to its durable JSONL. Splitting this ordering out from the wiring makes
   the drain-before-`stop_all` guarantee unit-testable with recording fakes.
9. **Signals and fatal conditions both trigger the same graceful path.**
   SIGINT/SIGTERM (via `loop.add_signal_handler`) and an auth rejection surfaced
   by the heartbeat loop (`on_auth_error`) both set one `asyncio.Event`; `run`
   awaits it and then drives `AgentServices.stop`.
10. **Result uploads are attempt-bound.** The production upload seam writes a
    retry copy under `results_dir`, posts the bytes to the coordinator, and
    checks the returned SHA-256 reference. The upload and completion requests
    both carry the lease attempt. Any upload acceptance or local persistence
    failure produces an internal deferred result, so the work loop reports no
    completion and lease expiry drives the retry. Transient failures retry with
    bounded exponential backoff when the next delay fits inside the lease. A
    verified upload removes the local copy.

## Consequences / limitations (honest)

- **Config is not hot-applied.** A pushed `AgentConfig` update (heartbeat
  interval, idle threshold, poll period) is logged and applied on the next
  restart, not live — the poll thread and heartbeat cadence are fixed at
  assembly. Live reconfiguration is future work.
- **Deferred payloads are recomputed on the next lease.** The local copy protects
  the bytes for diagnosis and later recovery work, but the current retry path
  reruns the unit after lease expiry. Reusing the saved computation is a future
  optimization.
- **`PreemptController` / `Preemptor` is a structural, not nominal, match.**
  `PreemptController` does not subclass the `Preemptor` ABC (unlike
  `ChildProcessSupervisor`, which does subclass `ProcessSupervisor`), so the
  runtime casts it at the two seams (`PollLoop`, `HeartbeatLoop`) that want the
  nominal ABC. Recorded as an open question against module A2.

## Open questions

- Content-addressed blobs that finish streaming after their lease changes are
  harmless but unreferenced. A later maintenance task should remove blobs that
  have no accepted binding.
- `PreemptController` does not inherit `fallow_protocol.interfaces.Preemptor`
  (`packages/fallow-agent/src/fallow_agent/preempt/controller.py:42`), forcing a
  cast where the runtime hands it to `PollLoop`/`HeartbeatLoop`.

## Testing

Fakes for idle, supervisor, preemptor, and model store; `httpx.MockTransport`
for register / heartbeat / manifest / input fetch; injected clocks and sleeps.
Covered: settings precedence and the `0.0.0.0` rejection; first-run
register-and-persist (0600) then load-and-skip; reconcile start/stop/defer and
STOPPED-restart; work-loop happy path and ACTIVE pause; graceful shutdown
ordering (drain before `stop_all`); port-allocator reuse; and a full
assembly build → enroll → graceful-shutdown integration pass.
