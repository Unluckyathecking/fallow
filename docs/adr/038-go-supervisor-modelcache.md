# ADR 038: Go process supervisor and model cache (E4.3)

**Status:** accepted

**Date:** 2026-07-15

## Context

The managed agent is being ported to Go (see [ADR 036](036-go-schema-codegen.md)
for the shared wire contract). The two lowest layers of the agent runtime are the
process supervisor (module A3, [ADR 003](003-process-supervisor.md)) and the model
cache (module A4, [ADR 004](004-model-cache.md)). Both must behave identically to
their Python originals: the supervisor drives the preemption hot path, and the
model cache shares a directory format that an operator may point either agent at.

The Python source is the specification. This ADR records the Go-specific
decisions taken during the port; the semantic decisions remain those of ADRs 003
and 004.

## Decision

### Suspend / resume per OS

The suspend primitive is a build-tagged seam (`suspendProcess`/`resumeProcess`):

- **Unix** sends `SIGSTOP` / `SIGCONT` to the child PID via `syscall.Kill`,
  matching `psutil.Process.suspend()/resume()`. The supervisor never starts a new
  session for a child, so signalling the single PID — not a process group — is
  the correct match for psutil's behaviour.
- **Windows** calls `NtSuspendProcess` / `NtResumeProcess` from `ntdll.dll` on a
  handle opened with `PROCESS_SUSPEND_RESUME`, which is what psutil uses on
  Windows. These suspend every thread of the process atomically; a partially
  suspended replica could still touch the GPU on the hot path. It is implemented
  with the standard-library `syscall` package (`NewLazyDLL`, `OpenProcess`,
  `CloseHandle`) rather than `golang.org/x/sys/windows`, so the Go module keeps
  zero third-party dependencies and no `go.sum`. The Windows file is compiled
  under `GOOS=windows` in CI cross-compilation; the test hosts are Unix.

The OS suspend seam is also injectable (`WithSuspendResume`) so tests can assert
state transitions without a real process where useful.

### Goroutine lifecycle vs Python threads

The Python supervisor runs one health *thread* per replica and detects process
death by polling `Popen.poll()`. Go's `os/exec` requires exactly one `cmd.Wait()`
call to reap a child, and `Wait` blocks. Each replica therefore has two
goroutines:

- a **reaper** goroutine owns the single `cmd.Wait()` and closes an `exited`
  channel when the process dies. A non-blocking read of that channel
  (`hasExited()`) is the exact analogue of `Popen.poll()` returning non-`None`;
- a **health** goroutine polls `/health` to `READY` (killing at `StartupTimeout`),
  then watches for unexpected death.

`StopReplica` closes the child's `shutdown` channel and joins the health goroutine
(bounded by `StopGrace + 1s`) before returning, so no goroutine outlives a stopped
replica. A single `sync.Mutex` guards the state maps and the cached status slice
only; every blocking call (spawn, syscalls, wait/kill, probes, joins) runs outside
the lock, preserving ADR 003's rule that the hot path never blocks under the lock.

### Byte-compatible cache layout

The Go `Store` writes the same files as the Python `HttpModelStore`:
`<model_id>/<file_name>`, `<file_name>.part`, and the `<file_name>.sha256` marker.
These names are a cross-language on-disk contract; an operator can share one cache
directory between a Python and a Go agent. The suffix constants live in one place
(`config.go`) and are exported so the contract is explicit.

### Range-resume and sha256 marker semantics

The download path mirrors the Python module exactly:

- rehash any pre-existing `.part` prefix once (it may predate the process), then
  send `Range: bytes=<size>-`;
- a `206` appends to the prefix; a `200` restarts from zero and discards the
  seeded prefix (the coordinator ignored the Range);
- after streaming, verify sha256 and size, write the marker, then `rename` the
  `.part` onto the final path (atomic publish);
- `PathIfPresent` trusts the marker and never rehashes the blob, keeping the
  heartbeat presence check O(1);
- transport failures and retryable statuses back off exponentially and surface as
  `ErrFetch`; a content mismatch deletes the `.part` and surfaces immediately as
  `ErrVerification`, never retried.

## Consequences

- The Go module remains dependency-free (standard library only), so the protocol
  boundary and its `go build`/`go test` gate stay simple and have no `go.sum`.
- The Windows suspend path compiles in CI cross-compilation but is not executed
  there; it is covered by code review against psutil's documented behaviour, not
  by an automated Windows test.
- The behavioural test cases from `test_a3_supervisor.py` and the three model-cache
  test modules are ported 1:1, so both languages are checked against the same
  observable contract. Real `sleep` subprocesses stand in for replicas; the
  suspend/resume test additionally inspects `/proc/<pid>/stat` on Linux.
- Marker-trust means external corruption of a verified blob is invisible until a
  launch fails — acceptable because Fallow solely owns the cache directory.
