# Inference process supervisor

`ChildProcessSupervisor` owns the inference processes started by one Fallow
agent. It launches replicas, waits for readiness, suspends them when the user
returns, resumes them when the machine is idle, and detects unexpected exits.

The supervisor implements `fallow_protocol.interfaces.ProcessSupervisor`. Its
caller chooses each port. The supervisor runs one replica per model ID.

## Public API

The package exports:

* `ChildProcessSupervisor`, with injected process, clock, health, and slot probe
  seams for deterministic tests
* `SupervisorConfig`, which holds the binary path, bind address, timeouts, and
  llama-server settings
* `CommandFactory` and `LlamaServerCommandFactory`
* `HealthCheck` and `SlotsCheck`
* `http_health_check`, `http_busy_slot_count`, and `parse_busy_slots`

## llama-server command

The command factory builds this shape:

```text
<binary> -m <model> --port <port> --host <bind_host>
         --parallel 2 -c 8192 <manifest arguments> --slots
```

GPU models also receive `-ngl 999 --flash-attn`. Values for parallelism,
context size, GPU layers, and the bind address come from `SupervisorConfig`.

The `--slots` flag is required by the pinned b4589 build. It appears after
manifest arguments so a stale `--no-slots` argument cannot disable occupancy
reporting.

llama-server has no authentication. The bind address defaults to loopback and
must never be `0.0.0.0`. Production agents use their tailnet address.

## Lifecycle

`start_replica` spawns the child without a shell and starts one daemon health
thread. That thread polls `/health` until the server returns 200. A startup exit
or timeout moves the replica to STOPPED.

After readiness, the same thread checks the process and polls `/slots` at
`health_poll_interval_s`. No extra occupancy thread is created. Suspended
replicas skip the slot request because their server cannot answer until the
process resumes.

`suspend_all` and `resume_all` use psutil outside the supervisor lock. Resume
restores the state recorded before suspension. `stop_replica` first asks the
child to terminate, waits for `stop_grace_s`, then kills it if needed.

STOPPED replicas remain visible in `statuses()` until the model starts again.

## Slot occupancy

The b4589 `/slots` response is a JSON array. Each entry must contain a boolean
`is_processing` field. The supervisor counts the true values and publishes the
result in `ReplicaStatus.inflight`. Heartbeats already carry this model, so the
wire message and protocol version stay unchanged.

The endpoint is optional and upstream describes it as unstable. A missing
endpoint, 501 response, timeout, malformed body, or unexpected probe exception
keeps the last valid count. A new replica begins at zero. The first failed probe
for a child writes a debug message; later failures stay quiet. Probe failure
never stops crash detection.

## Locking

One lock protects child maps, replica states, occupancy counts, and the cached
status tuple. Blocking operations never hold it. Process creation, psutil calls,
HTTP probes, process waits, and thread joins happen outside the lock.

The cached `statuses()` call only takes the lock long enough to return the tuple.
This keeps the user-return path independent of health and slot I/O.

## Tests

Supervisor tests launch harmless Python sleepers and inject probe fakes. Parser
tests cover the pinned slot shape and invalid responses. No test requires
llama-server or a GPU.

See [ADR 031](../../../../../docs/adr/031-slot-aware-inflight-routing.md) for the
routing decision and compatibility limits.
