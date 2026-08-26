# Changelog

All notable changes to Fallow will be documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases will follow Semantic
Versioning once public packages are published.

## [Unreleased]

### Added

- **LAN Site Mode**, an opt-in second deployment shape for a site with no tailnet
  and no internet. An on-site coordinator listens on one exact LAN address over
  HTTPS; Windows Go agents reach it outbound only, pinned to the SHA-256 of its
  certificate's SubjectPublicKeyInfo, and serve `llama-server` on loopback only,
  so no inbound firewall rule and no inference port on the LAN. Machines enrol
  from a per-device join file, `flw site join-bundles` mints them, `flw site
  status` shows the live fleet, and `agentctl doctor` / `deploy\windows\doctor.ps1`
  diagnose a desk without touching its state. Optional mDNS advertises the
  coordinator address as a recovery hint and never as trust. See
  [docs/lan-site/operator-runbook.md](docs/lan-site/operator-runbook.md) for the
  operator path and its honest gaps, and ADRs 078–096 for the decisions.

  Site Mode is additive and off by default. A coordinator without `[site].enabled`
  and an agent without `site_join_bundle` behave exactly as before: explicit URL
  and Tailscale deployments keep direct replica routing, tailnet binds and their
  existing trust model, unchanged.

- **A Site Mode desk install is now one artifact.** Every release carries
  `fallow-site-agent_<version>_windows_amd64.zip`: the released `agentctl.exe`, the
  Windows bootstrap, install, doctor, uninstall and llama-fetch scripts, an operator
  README, and a `manifest.sha256` covering all of it. A desk unzips that, stages
  llama.cpp, and runs `bootstrap.ps1 -JoinBundle <join file> -GoBinary .\agentctl.exe`,
  with no checkout of this repository on the machine. Model weights and llama.cpp are deliberately not in
  it; `fetch-llama.ps1` downloads the pinned build, or it is staged by hand on a desk
  with no internet. `deploy/site-bundle.sh build|verify` assembles and checks it with
  the same manifest discipline as the offline bundle, and CI builds and verifies one on
  every push. The bundle is unsigned and Windows-only. See
  [ADR 099](docs/adr/099-site-desk-bundle.md).

- **The coordinator installs as a systemd service on Linux.**
  `sudo deploy/coordinator/install.sh --ref v0.3.0` creates the `fallow` system user,
  checks the repository out at that pinned tag under `/opt/fallow/src`, builds the venv
  with `uv sync --frozen`, puts state in `/var/lib/fallow` and config in
  `/etc/fallow/coordinator.toml` (copied from the example only if absent, never
  overwritten), and installs `fallow-coordinator.service`, so the machine every desk
  depends on comes back after a reboot without anyone present. The run that seeds the
  config does not start the service, because that config still holds the example's
  placeholder admin key; edit it and re-run. Re-running it with a newer `--ref` is the
  upgrade, and it stops the running service before rewriting the checkout it runs from;
  `uninstall` removes the service and keeps state unless `--purge`. It refuses a branch
  without `--allow-branch`, refuses a `standby_path` the hardened unit could not write
  (`--allow-external-standby` once you have added the `ReadWritePaths` drop-in), and
  `--dry-run` prints the plan without touching the host. Requires root, `git`, `uv` and
  egress to github.com and PyPI. `uv sync` also downloads a managed CPython, so a
  zero-egress lab uses the offline bundle instead. Linux only: macOS coordinators keep
  the manual `serve` path. See
  [ADR 100](docs/adr/100-coordinator-systemd-install.md).

- **Windows desks can be installed remotely, from an elevated management context.**
  `deploy\windows\install.ps1 -User pilot -JoinBundle <file> -GoBinary .\agentctl.exe`
  runs as an admin or as SYSTEM — Intune, ConfigMgr, PDQ, a GPO startup script — and
  stages the agent, the join copy and a token-free config into the nominated account's
  profile (resolved from the ProfileList registry, so a relocated profile lands
  correctly), then registers that account's at-logon task. The agent still runs in the
  pilot user's own interactive session with `InteractiveToken` and `LeastPrivilege`,
  because idle detection needs that session; only the registration moves. Nothing is
  enrolled from the admin context and no token leaves the machine: the desk enrols on
  the user's next logon from the staged join file, as it always has, and the join copy
  is still readable by that account alone. It refuses an account that has never signed
  in (creating profiles is out of scope), a context that is not elevated, and a config
  it cannot read; `-WhatIf` rehearses the whole path. `uninstall.ps1 -User <account>
  [-Purge]` is the mirror. See
  [docs/pilot/remote-install.md](docs/pilot/remote-install.md) and
  [ADR 101](docs/adr/101-windows-admin-context-install.md).

### Changed

- The Go agent now reports the machine it runs on instead of placeholders. Enrollment
  carries real RAM, free disk, CPU model, OS version and, on Windows, the NVIDIA GPUs
  and their VRAM read through NVML; every heartbeat carries live CPU percentage,
  available memory and, on Windows, each GPU's free VRAM and utilisation. These are the
  figures `flw assign` and the fit endpoint read, so they agree with the fit the desk was
  placed by at enrolment. Capability-aware placement and automatic model selection were
  reading fixed values from Go-enrolled machines before this. The probes need no cgo
  and no new dependency, and each one degrades on its own to a conservative value,
  logged once, never fatal. See
  [ADR 097](docs/adr/097-go-host-metrics.md); the wire schema is unchanged.

- The LAN Site Mode pilot now places models by itself. `auto_assign_on_enroll` is on
  in `deploy/coordinator.example.toml` and in the runbook's pilot config, so a desk
  takes the largest registered model its own hardware can hold as it enrols rather
  than waiting for a per-machine `flw assign`, which is only trustworthy now that a
  Go agent enrols with real capacity. The runbook registers the model in §3, before
  the desks enrol: placement happens at enrolment and nowhere else. The default in code is unchanged,
  so the flag stays opt-in (ADR 048), and `flw assign` remains the override and is
  never overridden.

- **The desk installers now run on real hosts in CI.** The Windows Pester suite —
  join-file validation, the token-free config render, the ACL shape, the
  admin-context refusals — runs on `windows-latest` on every push, and a new
  `install-acceptance.yml` installs the desk bundle for real: a Scheduled Task
  registered both for the installing account and, with `-User`, for a nominated one,
  the staged files and their DACLs asserted, `doctor.ps1` parsed offline, then
  uninstalled clean. On `macos-latest` it does the same through `launchctl
  bootstrap gui/$UID`. The `(untested - verify on target)` markers on the steps those
  lanes exercise now say so; everything they cannot reach — the task starting at a
  real logon, EDR and SmartScreen, a real coordinator on a real LAN — keeps the old
  mark and stays a pilot-day check. See [ADR 102](docs/adr/102-install-acceptance-ci.md).

### Fixed

- The released macOS Go agent had no idle detection and so never yielded the machine
  to the person using it. Every target was cross-built with `CGO_ENABLED=0`, and macOS
  idle detection is a cgo call into Quartz, so the shipped binary carried the
  unsupported stub: it reported a fixed 300 s idle in every heartbeat and never ran the
  preemption state machine, which reads as permanently idle. `darwin/arm64` is now built
  natively with cgo on a macOS runner and published to the same release with the same
  archive name and checksums; Windows and Linux keep the cgo-less GoReleaser build.
  The daemon also fails closed: `agentctl run` refuses to start on a build with no idle
  detection, unless the config sets `assume_idle = true`: for test harnesses and
  dedicated headless hosts only, never a machine someone uses. Once running, a sample
  that fails reports the machine as in use rather than away, and `agentctl doctor` has
  an `idle` lane so a desk hears about it before it serves. See
  [ADR 098](docs/adr/098-go-idle-fail-closed.md).


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
