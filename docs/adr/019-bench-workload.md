# ADR 019: Bench workload generator (module B1)

Status: accepted · Date: 2026-07-15

## Context

Wave 4 runs a 3-arm scheduling experiment — (a) single dedicated machine, (b)
round-robin, (c) churn-aware v2 — and compares TTFT, tok/s, units/hour, recovery
time, time-to-yield, energy and % served on-prem. Every arm must be driven by
**the same client-side load** so the only variable is the coordinator's
scheduling policy. This module is that load driver: it fires interactive
requests at the OpenAI-compatible gateway, submits and polls one batch job via
the admin API, and samples per-agent power/state so the three arms produce
directly comparable datasets.

`fallow_bench` sits below everything and imports only `fallow_protocol` plus
third-party libraries (httpx, PyYAML). It never imports the coordinator, agent
or CLI: the workload talks to running coordinators strictly over HTTP, exactly
as a real client would.

## Decisions

1. **Open-loop arrival schedule, precomputed from the seed.** Inter-arrival gaps
   are drawn from an exponential distribution using `random.Random(seed)` and the
   full list of `(t_offset_s, prompt_idx, max_tokens)` is fixed **before** the run
   starts. Identical seeds therefore yield byte-identical schedules across arms.
   The driver fires each request at its scheduled offset and **never waits for the
   previous request to finish** (`asyncio.create_task`, no back-pressure). This is
   deliberate: a *closed*-loop driver would throttle itself on a slow arm and hide
   the very effect we are measuring. Under open loop, a slow arm accumulates
   queueing delay (`t_submit − t_scheduled` and TTFT grow as concurrency piles
   up) — that accumulation *is* the signal that distinguishes the arms.

2. **All clocks and randomness are injected.** A frozen `Clocks` seam bundles
   `monotonic` (schedule pacing), `now` (record timestamps) and `sleep`. The seed
   is config. Logic contains no `time.time()`, no `datetime.now()` and no
   unseeded RNG, so a run is replay-deterministic given `(config, clocks)`. The
   only real clock and the only environment reads live in `__main__` wiring. The
   per-request timeout uses `asyncio.wait_for` (loop time), matching the gateway's
   own streaming-timeout precedent (ADR 012 §5) — it is a real-time safety net,
   not part of the deterministic record path.

3. **Streaming, chunk-counted interactive requests.** Each request POSTs an
   OpenAI `chat/completions` body with `stream: true`; the driver counts SSE
   `data:` frames (excluding the `[DONE]` sentinel) as `tokens_out` and stamps
   `t_first_token` on the first frame. One JSONL line per request records
   `req_id, t_scheduled, t_submit, t_first_token, t_done, status, http_status,
   tokens_out, prompt_idx`; `status ∈ {ok, error, timeout}`.

4. **Three independent JSONL streams, one writer each.** `requests.jsonl`,
   `jobs.jsonl` and `power.jsonl` are written by three concurrent drivers, each
   owning its own append-and-flush writer. asyncio is single-threaded and writes
   are synchronous (no `await` between `write` and `flush`), so concurrent
   interactive request tasks never interleave a line. The resolved schedule is
   also dumped to `schedule.jsonl` and run metadata to `run.json`, so an arm's
   inputs are reproducible from its own run directory.

5. **The batch job is a client of the admin API.** `POST /v1/admin/jobs` submits
   a `JobSubmit` (wire type) at `submit_at_s`; the driver then polls
   `GET /v1/admin/jobs/{id}` every `poll_interval_s` until the job is terminal or
   the run's duration elapses. The 1 Hz sampler polls `GET /v1/admin/agents` and
   emits one `PowerSample` per (agent, GPU) — the software-side energy trace that
   feeds the "energy" and "% served on-prem" comparisons. Both use a small typed
   `BenchAdminClient` over an injected `httpx.AsyncClient`; tests drive it with
   `httpx.MockTransport` (no real network).

6. **Config is a frozen, YAML-loaded schema owned here.** `ExperimentConfig`
   (and its nested `InteractiveConfig`, `BatchConfig`, `SamplingConfig`) are
   `FallowModel`s (frozen, `extra="forbid"`), validated on load — an unknown or
   malformed key fails loudly. API/admin keys are referenced **by env-var name**
   (`api_key_env`, `admin_key_env`); the secrets themselves are read only at the
   `__main__` boundary, never stored in config or logs.

## Consequences

- The three arms share one schedule per seed, so cross-arm differences are
  attributable to scheduling, not to load jitter.
- `t_submit − t_scheduled` is a first-class column, making open-loop queueing
  delay directly measurable per arm.
- PyYAML ships no type stubs; the single `import yaml` carries a scoped
  `# type: ignore[import-untyped]` and its output is validated by pydantic. Adding
  `types-PyYAML` to the workspace dev group would remove the ignore (open
  question for whoever owns root tooling).
- `python -m fallow_bench run` dispatches to this module; a future `analyze`
  subcommand (module B0) will need to extend the same `__main__` shim.
