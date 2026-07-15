# Module A3 — inference process supervisor

`ChildProcessSupervisor` owns every fallow-launched inference child process
(llama-server, faster-whisper workers) on one machine. It spawns replicas,
health-gates them to `READY`, suspends/resumes them on the preemption hot
path, stops them gracefully, and detects unexpected deaths.

It implements `fallow_protocol.interfaces.ProcessSupervisor`. Port allocation
is **not** its job — the caller (heartbeat/main) picks the port and passes it in.

## Public API

Re-exported from `fallow_agent.supervisor`:

- `ChildProcessSupervisor(config, command_factory, *, health_check=, monotonic=, spawn=)`
  — the supervisor. Clocks and I/O are injected for deterministic tests.
- `SupervisorConfig` — frozen dataclass of all tunables (binary path, bind host,
  timeouts, llama args). See below.
- `CommandFactory` — `Protocol`: `(manifest, model_path, port) -> list[str]`.
  The command-construction seam.
- `LlamaServerCommandFactory` / `llama_server_command(config)` — the real
  llama-server argv builder.
- `HealthCheck` / `http_health_check` — the readiness-probe seam and its stdlib
  `http.client` default.

## llama-server command

`llama_server_command(config)` builds:

```
<binary> -m <model_path> --port <port> --host <bind_host>
         --parallel 2 -c 8192 <manifest.default_args...>
```

and, when `manifest.min_vram_mb > 0`, appends `-ngl 999 --flash-attn` for full
GPU offload. `--parallel`, `-c`, and `-ngl` values come from `SupervisorConfig`.

### Security: bind host is never `0.0.0.0`

llama-server has **no authentication**. `bind_host` defaults to `127.0.0.1`; in
production it is set to the machine's tailnet IP so the coordinator can proxy
inference over the tailnet. `SupervisorConfig` raises `ValueError` if you pass
`0.0.0.0` — binding to all interfaces would expose an open, unauthenticated
inference endpoint. Transport security is delegated to the tailnet (ADR 000).

## Lifecycle

```
start_replica ─▶ LOADING ─(GET /health == 200)─▶ READY
                   │                                │
   startup timeout │              suspend_all ⇄ resume_all
   or early exit   │                                │
                   ▼                          SUSPENDED
                STOPPED ◀─ stop_replica / crash / timeout ─┘
```

- `start_replica` spawns via `subprocess.Popen` (no shell) and starts one
  daemon **health thread** per child. The thread polls `GET /health` every
  `health_poll_interval_s` (default 500ms) until 200 → `READY`, or until
  `startup_timeout_s` (default 180s) → kill + `STOPPED`.
- After `READY`, the same thread keeps polling `popen.poll()`; a child that
  dies unexpectedly is detected and marked `STOPPED` (the reap loop).
- `suspend_all` / `resume_all` call `psutil.suspend()` / `resume()` (SIGSTOP /
  SIGCONT) on every live child. `resume_all` restores each replica's
  pre-suspend state (a replica suspended while still `LOADING` resumes to
  `LOADING`, not `READY`). Suspending an already-suspended child is a no-op; a
  vanished child (`NoSuchProcess`) is pruned to `STOPPED`, never an error.
- `stop_replica` / `stop_all` terminate, wait `stop_grace_s` (default 5s), then
  kill, reap the process, and join the health thread.
- `statuses()` returns a cached `tuple[ReplicaStatus, ...]` (`inflight` is
  always 0 — in-flight tracking arrives with the gateway).

## Invariants

- One replica per `model_id`; a duplicate `start_replica` for a live `model_id`
  is logged and ignored.
- `STOPPED` replicas remain visible in `statuses()` until a new `start_replica`
  reuses the `model_id`.
- The caller supplies the port; the supervisor never allocates one.

## Thread-safety and lock ordering

There is exactly **one** lock, `self._lock`. It guards only the in-memory maps
(`_children`, `_threads`, `_states`, `_ports`, `_pre_suspend`) and the cached
status tuple. The rule:

> **Never hold `self._lock` while doing blocking work.** Spawning, psutil
> suspend/resume, `popen.wait`/`terminate`/`kill`, `/health` probes, and
> thread `join`s all happen **outside** the lock.

Concretely, `suspend_all`/`resume_all` take the lock only to (1) snapshot the
live children and (2) commit the resulting states — the psutil syscalls run
between those two short critical sections. This keeps the preemption hot path
well under the 10ms budget for a handful of children and touches no network.
Because the lock is never held across a blocking call, there is no lock-ordering
hazard: the single lock can never be waited on while held.

## Testing seams

`health_check`, `monotonic`, and `spawn` are injected. Unit tests use real tiny
child processes (`python -c "time.sleep(60)"`) via an injected `CommandFactory`
and a fake health checker, so they need no network, no llama-server, and no GPU.
