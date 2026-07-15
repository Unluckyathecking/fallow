# ADR 010: Batch workers (A6)

Status: accepted · Date: 2026-07-15 · Module: fallow-agent/workers

## Context

The coordinator leases idempotent, content-addressed work units to an agent.
The agent must execute one unit against a **local** inference replica and report
a `WorkResult`. v0.1 has two workloads: `embed` (llama-server embeddings) and
`transcribe` (faster-whisper). This module depends on `fallow_protocol` only —
it never imports the supervisor or heartbeat — so leases are the sole input and
`WorkResult`s the sole output.

## Decision

1. **A thin `Worker` protocol + `WorkOutput`.** `async run(lease, input_bytes)
   -> WorkOutput(payload: bytes, metrics: WorkMetrics)`. Workers are pure
   "bytes → bytes over a local HTTP call"; concurrency, retries, and result
   accounting live above them in the runner.
2. **The runner owns the only clock and the only `try`.** `WorkUnitRunner`
   wraps fetch → select → run → upload in one `try`; ANY exception becomes a
   FAILED result with `error = "<Type>: <msg>"`. A worker bug can therefore
   never kill the agent. Workers emit `duration_s = 0.0`; the runner overwrites
   it with an injected-monotonic measurement.
3. **Injected seams, deterministic tests.** `fetch_input`, `upload`, the
   `httpx.AsyncClient`, the `EndpointResolver`, `tmp_dir`, and `monotonic` are
   all injected. Tests use `httpx.MockTransport` and plain fakes — no network,
   no llama-server, no faster-whisper, no GPU.
4. **Endpoint resolution is per-call, by `model_id`.** Workers take an
   `EndpointResolver(model_id) -> LocalEndpoint`. Which local replica serves a
   model is a wave-3 assembly concern; the worker just dials what it is handed.
5. **Optional deps fail at construction.** `TranscribeWorker` imports
   faster-whisper lazily behind a monkeypatchable seam and loads its model in
   `__init__`; a missing `[whisper]` extra raises `WorkerUnavailableError`
   there, not at run time, so scheduling can drop the `transcribe` kind rather
   than lease units that would always fail.
6. **Immutable `WorkerRegistry`.** `register` returns a new registry; factory
   closures carry the shared client / resolver / tmp dir. Construction errors
   (e.g. `WorkerUnavailableError`) propagate to the assembly, which decides
   which kinds the runner actually holds.

## Consequences

- One poisoned unit or a replica outage degrades to a single FAILED result; the
  coordinator's retry/DEAD budget handles the rest.
- CPU-bound transcription blocks its calling task; the assembly must run each
  lease on its own task/thread (documented, out of scope here).
- Result upload and input fetch transports are out of scope — injected — so this
  module stays a leaf that only knows local replicas and `fallow_protocol`.
