# ADR 017: Deployment — binary staging + per-machine service install (module I4)

Status: accepted · Date: 2026-07-15

## Context

Wave 0–2 produced a working coordinator and agent, but nothing installs them onto
real office machines or stages the `llama.cpp` binary the agent's process
supervisor launches. Two hard constraints shape this module:

1. **Idle detection needs the logged-in GUI session.** The agent reads the
   console user's HID idle timer — `CGEventSourceSecondsSinceLastEventType`
   (macOS, via pyobjc Quartz) and `GetLastInputInfo` (Windows, user32). Both only
   return meaningful values inside the **active interactive session**. A macOS
   LaunchDaemon (session 0) or a Windows Service (session 0) sees no input desk
   and would read "always idle", so Fallow would never yield to the user —
   defeating ADR 000 §3 (instant preemption, "users must never notice Fallow").
2. **`llama.cpp` ships no per-asset checksums, and its Windows CUDA build omits
   the CUDA runtime DLLs.** The missing-`cudart64_*.dll` failure at
   `llama-server.exe` launch is the single most common Windows setup trap.

This module is deploy scaffolding — shell/PowerShell + service manifests + a
README. It contains **no package code** and imports nothing, so it sits outside
the import-linter DAG.

## Decision

- **Agents install as user-session background jobs, never system services.**
  macOS: a per-user **LaunchAgent** loaded with `launchctl bootstrap gui/$UID`
  (`KeepAlive`, `RunAtLoad`, `ProcessType=Background`). Windows: an **at-logon
  Scheduled Task** with `LogonType=InteractiveToken`, `RunLevel=LeastPrivilege`,
  `pythonw.exe` (no console), and `RestartOnFailure`. The "why not a
  daemon/service" rationale is written into both manifests and the README so it
  survives future edits.
- **The llama.cpp release is pinned in one variable** at the top of each fetch
  script (`LLAMA_RELEASE`; Windows adds `CudaTag`). Because upstream publishes no
  `SHA256SUMS`, the scripts **record** the downloaded SHA256 into
  `deploy/llama-version.lock` on first run and **verify against it** thereafter;
  the lockfile is committed so every machine pins identical bytes.
- **Windows fetch downloads BOTH the CUDA build and the matching `cudart-…`
  archive** and unpacks them into the same directory, with the trap called out
  loudly. The two archives' CUDA sub-version must match.
- **v0.1 install story = git checkout + `uv sync`.** Fallow is not on PyPI, so
  installers build a `uv`-managed `.venv` in a local checkout and point the
  service at `.venv/bin/python` (macOS) / `.venv\Scripts\pythonw.exe` (Windows).
  Config is copied from the example TOML (owned by the config module) to
  `~/.fallow/agent.toml` only if absent — a live config is never clobbered.
- **Service manifests are templates.** `com.fallow.agent.plist` and
  `fallow-agent-task.xml` carry `__TOKEN__` placeholders that the installers fill
  with resolved absolute paths (interpreter, config, logs, working dir, user id),
  because neither `launchd` nor Task Scheduler expands `~`/env vars in those
  fields.
- **Tailscale is a documented hard prerequisite** (ADR 000 §6). Replica
  `bind_host` is the machine's tailnet IP; `0.0.0.0` is rejected by the supervisor
  config. Zero-egress labs pre-stage models on the coordinator (`flw models
  pull`); agents pull blobs from the coordinator over the tailnet.
- **Honesty markers, not confident prose.** This module was authored with no
  network and no macOS/Windows service host, so every download / `launchctl` /
  `Register-ScheduledTask` step is annotated `(untested — verify on target)` in
  the scripts and the README.

## Consequences

- The agent's liveness is bound to an interactive login: it starts at login and
  dies at logout/fast-user-switch away. That is correct for idle-aware compute
  (no user session ⇒ nothing to yield to) but means a headless/locked-console
  machine contributes nothing in v0.1.
- Unsigned `llama-server.exe` + socket-binding `pythonw.exe` will trip
  Defender/SmartScreen/EDR in managed fleets; the README frames this as an
  organizational allowlisting task with real lead time, ideally a hash-pinned
  AppLocker/WDAC rule keyed to `llama-version.lock`.
- Because upstream ships no checksums, the first fetch is trust-on-first-use; the
  lockfile only protects against later drift. Verifying the pinned tag/asset names
  against the releases page before first use is a manual gate.
- Coordinator service management (systemd/launchd) is intentionally left to the
  operator in v0.1; only the agent is fully scripted.

## Alternatives considered

- **System service everywhere** — rejected: breaks idle detection (session 0).
- **Bundle `llama-server` in the repo / a wheel** — rejected: large per-platform
  binaries, licensing, and staleness; a pinned fetch + lockfile is leaner.
- **Depend on an upstream checksum file** — not available from llama.cpp releases;
  hence the record-then-verify lockfile.
