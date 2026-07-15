# Changelog

All notable changes to Fallow will be documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases will follow Semantic
Versioning once public packages are published.

## [Unreleased]

### Added

- `UnitTransition` as the shared contract for committed lease, completion, requeue, and
  dead-unit events.
- Coordinator `units.jsonl` output with per-unit agent, attempt, state, and time fields.
- Attempt-bound result payload uploads, coordinator-side content-addressed storage,
  and authenticated admin retrieval.
- A bounded FIFO admission queue that waits up to 10 seconds for an interactive replica.
- Canonical scheduling experiments with three arms, three paired seeds, two-hour live
  runs, and 120-second smoke runs.
- Isolated per-run coordinator templates, canonical metadata and artifacts, an explicit
  baseline phase, collision refusal, and a warning-free smoke-to-analysis path.
- Separate dedicated and distributed fleet snapshots, immutable churn-history input,
  bounded fleet readiness checks, and coordinator secrets supplied only at process start.
- A paper skeleton with the fixed study question and method, B3 result slots for each
  paired seed, and threats to validity recorded before the live runs.

### Changed

- The analysis default for unit lifecycle input is now `units.jsonl` instead of
  `job_status.jsonl`.
- Churn records include optional `t_epoch` values so recovery analysis can compare them
  with coordinator timestamps. Older replay offsets remain readable through
  `run_meta.json.started_at`.
- Agent upload failures now leave the lease incomplete for retry instead of recording a
  terminal failed result. Retry bytes remain on the agent until the coordinator confirms
  the expected digest.
- Gateway request records include `waited_ms` for served and shed requests.

## [0.1.0] - 2026-07-15

First tagged release: the full system runs live on a two-machine fleet. Pre-alpha —
suitable for development and research only, not for production or high-risk use.

### Added

- **Protocol (`fallow-protocol`).** Frozen pydantic wire models and interface ABCs behind
  a pydantic-plus-stdlib portability boundary; `PROTOCOL_VERSION` exchanged at
  registration; JSON Schemas exported to `schemas/` and diff-checked in CI.
- **Agent (`fallow-agent`).** Cross-platform idle detection, a dedicated-thread preemption
  state machine, an inference process supervisor, a resumable verifying model cache, the
  heartbeat/uplink client, batch workers (`embed`, `transcribe`), and the `run`
  composition root.
- **Coordinator (`fallow-coordinator`).** WAL-SQLite registry and durable work-unit queue,
  three config-selectable scheduler arms (`capability`, `roundrobin`, `churn_v2`), a
  Range-capable model-blob server, an OpenAI-compatible streaming gateway with per-request
  `gateway.jsonl` logging, and the `serve` app factory.
- **CLI (`fallow-cli`).** The `flw` operator client and the admin API contract in
  `docs/admin-api.md`.
- **Composition & tests.** End-to-end integration/chaos suite (332 passing tests across the
  workspace) covering lifecycle, batch jobs, churn recovery, preemption and gateway
  streaming; deployment scripts that stage a pinned `llama.cpp` and install agents in the
  logged-in GUI session.
- **Docs.** ADRs 000–021, architecture overview, the scheduling-experiment protocol,
  community-health files, and compatibility/stability/release policies.

### Validated (live two-machine demo, 2026-07-15)

Coordinator + agent on a MacBook Air (Apple Silicon) and an agent on Windows 11 / RTX
3070, over Tailscale, serving Qwen2.5-0.5B-Instruct Q4_K_M via llama.cpp. Evidence in
`experiments/spikes/RESULTS.md` (`events.jsonl` / `gateway.jsonl`):

- Full pipeline: enrollment-token registration → heartbeats → model assignment → agents
  pulled the 491 MB blob from the coordinator (sha256-verified) → replicas launched (CUDA
  on the PC, Metal on the Mac) → READY.
- Real preemption in production: the Mac user returned mid-session and the agent suspended
  its replica in **1.268 ms**, then auto-resumed after the 120 s idle threshold.
- End-to-end yield p99 under full CPU load: **103 ms** (Mac) / **116 ms** (Windows) — 2.6×
  inside the 300 ms budget.
- Gateway streaming: OpenAI-compatible SSE with a warm end-to-end **TTFT of 222 ms**.
- Machine-death failover: a hard-killed PC agent caused **zero failed client requests** —
  every request routed to the surviving Mac replica.

### Security

- Documented the trusted-network (tailnet) assumption, the three bearer-token identities
  plus admin key, and the explicit blast radius of a compromised worker
  ([docs/architecture.md](docs/architecture.md)). No production security audit yet.

### Fixed

- Gateway first-byte timeout: an httpx transport `read` timeout could fire while awaiting a
  cold replica's first token; the transport read is now a backstop above the app-level
  first-byte/inter-chunk `wait_for` guards (found and fixed during the live demo).
- Avoid signalling an already-exited supervised child, including the Windows process-handle
  behaviour where a reaped process can otherwise surface as access denied.
