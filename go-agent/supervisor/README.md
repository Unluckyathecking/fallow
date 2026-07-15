# supervisor

The Go process supervisor owns every fallow-launched inference child process
(llama-server, faster-whisper workers). It spawns them, gates them to `READY`
with an HTTP `/health` probe, suspends and resumes them instantly on the
preemption hot path, and stops them gracefully.

This package is a port of the Python `fallow_agent.supervisor` package and keeps
the same lifecycle semantics. See [ADR 003](../../docs/adr/003-process-supervisor.md)
for the original design rationale and [ADR 038](../../docs/adr/038-go-supervisor-modelcache.md)
for the Go-specific decisions.

## Public API

```go
cfg := supervisor.DefaultConfig("/opt/llama/llama-server")
cfg.BindHost = "100.64.0.1" // tailnet IP; 0.0.0.0 is rejected

sup, err := supervisor.New(cfg, supervisor.LlamaServerCommand(cfg))
if err != nil { /* invalid config */ }

sup.StartReplica(manifest, modelPath, port) // spawn + health-gate to READY
sup.SuspendAll()                            // hot path: SIGSTOP every replica
sup.ResumeAll()                             // restore pre-suspend state
sup.Statuses()                              // snapshot of every known replica
sup.StopReplica(modelID)                    // graceful terminate, then kill
sup.StopAll()
```

One replica per `model_id`; port allocation is the caller's responsibility.

## Concurrency model

A single `sync.Mutex` guards the state maps (`children`, `states`, `ports`,
`gpu`, `preSuspend`, `healthDone`) and the cached `[]ReplicaStatus` slice **only**.
Every blocking operation runs outside the lock:

- spawning a process,
- the suspend/resume syscalls,
- process wait / terminate / kill,
- `/health` probes,
- goroutine joins.

That is what keeps `SuspendAll`/`ResumeAll` fast on the preemption hot path: they
take the lock just twice — once to snapshot the live children, once to commit the
new states — and run the syscalls in between. The hot path never spawns, never
blocks on I/O, and never touches the network.

The Python implementation runs one health *thread* per replica; here each replica
has two goroutines:

- a **reaper** goroutine that calls `cmd.Wait()` (reaping the OS process) and
  closes an `exited` channel — the non-blocking `hasExited()` probe reads that
  channel, mirroring `Popen.poll()`;
- a **health** goroutine that polls `/health` until `READY` (or kills the replica
  at `StartupTimeout`), then watches for unexpected death (`STOPPED`).

`StopReplica` closes the child's `shutdown` channel and joins the health goroutine
before returning, so no goroutine outlives a stopped replica.

## Suspend / resume per OS

The suspend/resume primitive is behind a build-tagged seam:

- **Unix** (`suspend_unix.go`): `SIGSTOP` / `SIGCONT` to the child PID, matching
  `psutil.Process.suspend()/resume()`. The supervisor never puts children in a
  new session, so signalling the single PID is correct (no process-group fan-out).
- **Windows** (`suspend_windows.go`): `NtSuspendProcess` / `NtResumeProcess` from
  `ntdll.dll`, opened with `PROCESS_SUSPEND_RESUME`. These act on every thread of
  the process atomically — the same primitive psutil uses on Windows. It is
  built and compiles under `GOOS=windows`; the CI and local test hosts are Unix.

## Injected seams

Like the Python version, every source of nondeterminism is injectable through
functional options, so tests drive real tiny subprocesses with no HTTP or GPU:

- `WithHealthCheck` — the readiness probe (default `HTTPHealthCheck`).
- `WithSpawn` — the process spawner (default discards stdio to the null device).
- `WithClock` — the monotonic clock (default `time.Now`).
- `WithSuspendResume` — the OS suspend/resume seam (default is the build-tagged
  platform primitive).

## Notes

- `bind_host` defaults to loopback and rejects `0.0.0.0`: llama-server is
  unauthenticated, so binding to all interfaces would expose an open inference
  endpoint.
- `ReplicaStatus.Inflight` is always `0` until the gateway lands (a later wave),
  matching the Python stub.
- Crash detection is eventual, bounded by `HealthPollInterval` (default 500ms).
