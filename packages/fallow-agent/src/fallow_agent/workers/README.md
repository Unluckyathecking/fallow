# fallow-agent · workers (A6)

Batch workers. Given a `WorkUnitLease`, run the unit against a **local**
inference replica and produce a `fallow_protocol.messages.WorkResult`. This
module depends on `fallow_protocol` **only** — leases in, results out. Workers
dial local replica HTTP ports; they never import the supervisor or heartbeat.

## Purpose

The coordinator leases idempotent, content-addressed work units to an agent.
This module executes one unit at a time: fetch its input, pick the worker for
`lease.kind`, call the local replica, upload the result, and report SUCCEEDED or
FAILED. A worker bug must never take the agent down.

## Public API

Re-exported from `fallow_agent.workers`:

- `Worker` — `async run(lease, input_bytes) -> WorkOutput` protocol.
- `WorkOutput` — frozen `(payload: bytes, metrics: WorkMetrics)`.
- `EmbedWorker` — batch embeddings via a local `/v1/embeddings` replica.
- `TranscribeWorker` — speech-to-text via faster-whisper (optional `[whisper]`).
- `WorkerRegistry` — immutable `WorkerKind -> WorkerFactory` map.
- `WorkUnitRunner` — drives one lease to a `WorkResult`.
- `EmbedConfig`, `TranscribeConfig` — frozen tuning.
- Errors: `WorkerError`, `WorkerUnavailableError`, `WorkerNotRegisteredError`,
  `WorkerInputError`, `WorkerBackendError`.

### EmbedWorker

```python
worker = EmbedWorker(
    client=httpx.AsyncClient(...),          # injected; caller owns lifecycle
    resolve_endpoint=lambda model_id: LocalEndpoint("127.0.0.1", 8081),
    config=EmbedConfig(),                    # optional
)
```

- Input bytes: a JSON array of strings (chunks).
- Calls `POST http://{host}:{port}/v1/embeddings` with
  `{"model": model_id, "input": chunks}` (OpenAI-compatible).
- Output payload: `{"embeddings": [[...]], "model_id": ..., "dims": N}`.
- `metrics.items` = chunk count; `metrics.tokens` = `usage.total_tokens` if the
  replica reported it.

### TranscribeWorker

```python
worker = TranscribeWorker(
    config=TranscribeConfig(model_size_or_path="base"),
    tmp_dir=Path("/tmp/fallow-audio"),
    loader=default_whisper_loader,           # injectable seam
)
```

- Input bytes: an audio segment file; written to `tmp_dir`, deleted after the run.
- Output payload: `{"text": ..., "segments": [{"start", "end", "text"}]}`.
- `metrics.items` = segment count.
- **faster-whisper is optional.** If the `[whisper]` extra is not installed the
  constructor raises `WorkerUnavailableError` — never at run time — so the
  assembly can drop the `transcribe` kind and the scheduler avoids leasing it.

### WorkUnitRunner

```python
runner = WorkUnitRunner(
    workers={WorkerKind.EMBED: worker},      # instances, built once
    fetch_input=async_fetch,                 # (lease) -> bytes
    upload=async_upload,                     # (lease, payload) -> result_ref
    monotonic=time.monotonic,                # injected clock
)
result = await runner.run_lease(lease)
```

## Invariants

- **No worker bug kills the agent.** Fetch, worker, and selection failures become
  a FAILED `WorkResult` whose error is `"<ExceptionType>: <message>"`. A deferred
  upload returns `DeferredWorkResult` instead. The work loop reports nothing for
  that lease, which prevents an unavailable result store from turning completed
  computation into a terminal queue result. Transient uploads retry with bounded
  backoff while the lease still has enough time.
- **The runner owns the clock.** Workers set `metrics.duration_s = 0.0`; the
  runner overwrites it with the measured wall-clock duration (never negative).
- **Availability fails at construction, not run.** `TranscribeWorker` loads its
  model in `__init__`; a missing backend raises `WorkerUnavailableError` there.
- **`WorkerRegistry` is immutable.** `register` returns a new registry; the
  factory closure carries the shared client / endpoint resolver / tmp dir.
- **Local-only egress.** Workers dial replica ports on the loopback / tailnet
  interface; they never call the coordinator directly (fetch/upload are injected).

## Threading note

`TranscribeWorker.run` is `async` but its faster-whisper decode is CPU-bound and
blocks the calling task. The wave-3 assembly is expected to run each lease on its
own task/thread so one long transcription never stalls other agent work. Wiring
that offload is out of scope for this module.

## Out of scope

- Choosing which local replica serves a `model_id` (the injected
  `EndpointResolver` / `resolve_endpoint` does that in wave-3 assembly).
- Choosing the result transport. HTTP upload remains an injected callable.
- Chunking / job splitting (coordinator-side).
