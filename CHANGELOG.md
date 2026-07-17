# Changelog

All notable changes to Fallow will be documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases will follow Semantic
Versioning once public packages are published.

## [Unreleased]

## [0.3.0] - 2026-07-17

School-pilot-ready milestone. The agent now installs and runs unattended on managed
Windows and macOS machines, the coordinator can hand a serving fleet to a warm standby,
and an operator can reclaim any machine on demand. Still pre-alpha and intended for a
single supervised pilot, not general production. Deploy the pinned `v0.3.0` tag, not
`main` (see [docs/releasing.md](docs/releasing.md)).

### Added

- Hardened Windows and macOS agent installers that detect the CUDA, Metal, or CPU backend
  and verify the downloaded llama.cpp build against a signed SHA-256 manifest before use.
- A one-shot bootstrap installer (`deploy/bootstrap.sh`, `deploy/bootstrap.ps1`) that wraps
  the per-OS installers for a single-command agent setup.
- [docs/school-pilot.md](docs/school-pilot.md), an IT-facing readiness page covering the
  network, identity, and data-handling assumptions for a school deployment.
- A Phase-A pilot acceptance-test harness that drives the enrollment-to-serving path against
  the pilot acceptance criteria.
- Coordinator warm-standby export and a manual `promote` command to bring a standby online.
- Instant reclaim / kill-switch: an operator can suspend and evict a machine's replica on
  demand, and the machine returns to idle.
- Experimental, off-by-default peer model distribution (`fallow-modelmesh`): content-addressed
  chunks served under a coordinator-signed manifest, opt-in per agent, with automatic fallback
  to the direct blob download. The blob download stays the default and is unchanged when the
  mesh is off.
- A versioned RAG vector store with fixed-dimension collections, transactional
  chunk upserts, and deterministic nearest-neighbor queries through sqlite-vec.
- Admin RAG ingestion routes that submit content-addressed chunks as fleet embed
  jobs and finalize accepted payloads through an injected vector-store seam.
- An API-key-authenticated RAG query route that uses a live fleet embedding
  replica and returns ranked chunks with source metadata and L2 scores.
- A Go agent module with generated protocol types and shared Python and Go JSON
  conformance fixtures.
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
- Optional per-key RPM and UTC-day request limits, OpenAI-shaped 429 responses, and
  fixed-interval registry snapshots for quota recovery after restart.
- A double-gated benchmark-only constant idle detector for dedicated Linux experiment hosts.
- Provider-neutral fleet rendering, validation, offline dry-run, setup, and cleanup scripts.
- A paper skeleton with the fixed study question and method, B3 result slots for each
  paired seed, and threats to validity recorded before the live runs.

### Changed

- Relicensed the workspace from Apache-2.0 to AGPL-3.0-or-later.
- The analysis default for unit lifecycle input is now `units.jsonl` instead of
  `job_status.jsonl`.
- Churn records include optional `t_epoch` values so recovery analysis can compare them
  with coordinator timestamps. Older replay offsets remain readable through
  `run_meta.json.started_at`.
- Agent upload failures now leave the lease incomplete for retry instead of recording a
  terminal failed result. Retry bytes remain on the agent until the coordinator confirms
  the expected digest.
- Gateway request records include `waited_ms` for served and shed requests.

### Fixed

- The gateway admission queue now measures `waited_ms` with `time.perf_counter`
  instead of `time.monotonic`, so short waits are reported accurately on Windows
  under Python 3.12, where `time.monotonic()` has ~15.6 ms resolution.

### Security

- Reconciled the transport-security docs with the tailnet trust model in ADR 052. The
  trusted-network assumption and bearer-token identities are unchanged; the docs now match
  the shipped behaviour.

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
