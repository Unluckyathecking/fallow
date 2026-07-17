# ADR 062: One-shot bootstrap installer

**Status:** Accepted

**Date:** 2026-07-17

## Context

Enrolling a machine today means reading the deploy README, staging the
llama.cpp binary, picking the right backend for the hardware, running the per-OS
installer with the correct flags, and getting an enrollment token into the
agent's first run without leaving it on disk. That is fine for the person who
wrote it and too much for a school IT team provisioning a room of machines.

The per-OS installers (`deploy/macos/install.sh`, `deploy/windows/install.ps1`)
are already hardened: two flavours (Python venv, prebuilt Go binary), backend
detection on macOS, SHA256 verification against a signed manifest before any
binary runs, idempotent service wiring. ADR 061 covers that work. What is
missing is a single front door that reads the machine and drives those
installers, so the operator runs one command instead of a checklist.

## Decision

Add two sibling entry points — `deploy/bootstrap.sh` (POSIX `sh`) and
`deploy/bootstrap.ps1` (Windows PowerShell) — that orchestrate the existing
installers. They are not a second installer. They detect, select, and delegate;
every side effect that matters (venv build, manifest verification, service
registration) stays in `install.sh` / `install.ps1` and is neither copied nor
relaxed here.

Detection and backend selection. Each script reads the OS, CPU arch, RAM, and
GPU. Apple Silicon selects Metal, Intel Mac selects CPU (there is no CUDA on
macOS), an NVIDIA Windows machine selects CUDA, and anything else selects CPU
with a warning — the shipped Windows llama.cpp build is CUDA-only, so a
GPU-less Windows host is flagged rather than quietly misconfigured. On macOS the
choice is passed through as `FALLOW_INSTALL_BACKEND`, which `install.sh` already
reads; the bootstrap adds no new backend plumbing.

Delegation. The bootstrap forwards the flavour it was asked for — the Python
checkout path, or `--go-binary` / `-GoBinary` for the prebuilt agent — straight
to the installer. Verification is the installer's job and happens exactly as
before: the bootstrap never sees a binary hash and never decides whether a
binary is trusted.

Enrollment secret handling. The one-time token comes from `--token` / `-Token`
or `FALLOW_ENROLLMENT_TOKEN`. It is held in memory and never written to a file.
On macOS it is placed in the launchd session environment with `launchctl
setenv`, the LaunchAgent is restarted so it inherits the token and registers,
and the token is removed with `launchctl unsetenv` once the agent has persisted
its identity. On Windows, where a Scheduled Task cannot inherit an in-memory
session variable, the token is set on a single foreground enrollment process's
environment, which registers and exits, after which the variable is cleared. In
both cases the agent persists only its identity (`agent-state.json`, 0600) and
never the token, so no secret survives the run; the bootstrap additionally
refuses to finish if the token string is found in `agent.toml`. Enrollment is
skipped when no token is given (re-install of an already-enrolled machine) and
when an identity already exists.

Self-test. After install the bootstrap checks observable state without touching
the network: the service is loaded (`launchctl print` / `Get-ScheduledTask`) and
the config file is in place. It reports success or failure and exits non-zero on
failure.

Dry run. `--dry-run` / `-WhatIf` reports the detection result and delegates to
the installer's own preview (`FALLOW_INSTALL_DRY_RUN` / `-DryRun`), which renders
the service definition and runs the same verification without creating anything.
It performs no enrollment and no self-test. This is the path the acceptance
harness drives, and it changes nothing on the machine.

## Consequences

Provisioning a machine is one command. The bootstrap owns detection, backend
selection, token routing, and the self-test; the installers keep sole ownership
of building, verifying, and wiring the agent, so there is one implementation of
each of those, not two.

The two scripts stay separate per OS because launchd and Task Scheduler share no
mechanism worth abstracting — the macOS token path uses `launchctl setenv`, the
Windows path a foreground run, and each is the idiomatic minimal choice for its
service manager. The foreground enrollment path reconstructs the agent's run
command, which mirrors a few lines of the installer; the comment there flags it
so the two stay in step.

`bootstrap.sh` passes shellcheck as `sh`. Both scripts were authored in a
sandbox with no target host, so the install, enrollment, and self-test steps
that reach launchd, Task Scheduler, and the coordinator are marked
(untested — verify on target), consistent with the rest of `deploy/`.
