# ADR 021: Bench metrics analysis (module B3)

Status: accepted · Date: 2026-07-15

## Context

Wave 4 runs a 3-arm scheduling experiment — (a) single dedicated machine, (b)
round-robin, (c) churn-aware v2 — and must report one headline table plus a few
plots across arms. Each arm produces a *run directory* of append-only JSONL logs
emitted by other modules: the B1 client trace and power sampler, the
coordinator's `gateway.jsonl` (`GatewayLogEntry`) and `events.jsonl`
(`AgentEvent`), the B2 `churn.jsonl`, and the batch job-status log. B3 is the
offline reducer: logs in → `report.md` / `report.tex` / three PNGs out. It runs
after the experiment, never during, and must be replay-deterministic so a paper
figure regenerated from the same logs is byte-identical.

## Decision

- **Layer-clean, structural parsing.** B3 lives in `fallow_bench.analysis` and
  imports only `fallow_protocol` + third party (pandas/numpy/matplotlib), per the
  import-linter DAG. It never imports `fallow_coordinator`, so `GatewayLogEntry`
  and `AgentEvent` are parsed *structurally* from JSON (field names read off the
  producer models, not the classes). Field names for the not-yet-built B1/B2
  producers are pinned here and surfaced as open questions for those owners.
- **Loaders are pure and total.** One loader per file kind, each
  `Path → (DataFrame, warnings)`. A missing file yields an empty frame *with the
  expected columns* plus a warning; a malformed line is skipped with a warning.
  A loader never raises on bad input — the pipeline degrades to blank metrics
  (`—`) rather than crashing a batch report job.
- **Metrics are pure functions frame(s) → `float | None`.** `None` (rendered
  `—`) is the honest answer when the inputs are absent, so every metric composes
  over partial runs. Percentiles use `numpy` linear interpolation
  (`method="linear"`), the single definition used across TTFT / decode / yield /
  recovery so the table is internally consistent and hand-checkable.
- **Determinism is designed in, not hoped for.** No wall-clock and no unseeded
  randomness: the idle-baseline window is *declared in config*, run metadata
  (title, git sha, notes) is *passed in* (`ReportMeta`), arms are emitted in
  sorted label order, and every float is formatted at a fixed precision. Running
  `analyze` twice over the same inputs produces byte-identical `report.md` and
  `report.tex`. PNGs (matplotlib Agg) are explicitly exempt from the byte
  guarantee.
- **Energy is a marginal integral.** Per agent, trapezoidally integrate
  (`numpy.trapezoid`) `watts(t) − baseline`, where `baseline` is that agent's
  mean draw inside the declared idle window; sum across agents and divide by
  client-trace tokens to get joules per 1000 tokens. This charges the experiment
  only for the *work-induced* power over the machine's idle floor.
- **Failure-recovery joins churn kills to re-completions.** A killed agent's
  leased unit that later reaches `done` on a *different* agent contributes
  `t_done − t_kill`; the reported figure is the median over such incidents. This
  needs per-unit agent+state+time rows in the job-status log (schema pinned
  below).

## Consumed JSONL schemas (one object per line)

- **client trace** (B1): `req_id, t_submit, t_first_token?, t_done, status,
  tokens_out`. Times are epoch seconds (float) or ISO-8601; `status == "ok"`
  counts toward latency/decode.
- **gateway** (`GatewayLogEntry`): only `status ∈ {served, shed, error}` is read.
- **events** (`AgentEvent`): `kind, at, detail`; `kind == "user_returned"`
  contributes `float(detail["yield_ms"])`.
- **churn** (B2): `t, agent_id, action`, optional `flip_latency_ms` (added to the
  time-to-yield sample) and `action == "kill"` marks a failure instant.
- **power** (B1): `t, agent_id, watts`.
- **job status**: `work_unit_id, job_id, agent_id, attempt, state ∈ {leased,
  done, dead}, t`.

## Table rows (one column per arm)

TTFT p50 (s), TTFT p95 (s), decode tok/s p50, batch units/hour, failure-recovery
(s), time-to-yield p50 (ms), time-to-yield p99 (ms), marginal energy per 1k
tokens (J), % served on-prem.

## Consequences

- The report degrades gracefully: an arm missing `power.jsonl` still gets every
  other row, with energy shown as `—` and a warning collected — no arm can crash
  the cross-arm table.
- B3 pins the B1/B2/job-status on-disk field names before those producers exist;
  if they diverge, the fix is one loader's column map, not the metrics. The
  pinned names are the open questions below.
- A top-level `fallow_bench/__main__.py` subcommand dispatcher is introduced so
  `python -m fallow_bench analyze …` works; it is a thin router that B1/B2 extend
  with their own subcommands.

## Open questions

- Exact on-disk field names for the B1 client trace + power sampler and the B2
  churn log are pinned by this ADR but owned by those modules; confirm on
  integration. In particular whether churn emits `action == "kill"` and an
  optional `flip_latency_ms`, and whether the job-status log is per-unit (assumed
  here) or per-job snapshots (`JobStatus`, which lacks the per-unit timing the
  recovery join needs).
- `units/hour` derives its elapsed window from the job-status time span; a
  dedicated observation-window field in run config would be less sensitive to the
  first/last record and is deferred.
