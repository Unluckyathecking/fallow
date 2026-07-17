# ADR 060: Windows agent installer hardening for the school-IT pilot

**Status:** Accepted

**Date:** 2026-07-17

## Context

The Windows installer under `deploy/windows/` was written for a developer with
an NVIDIA machine. It always fetched the CUDA llama.cpp build, recorded the hash
of whatever it first downloaded (trust on first use), and registered the
scheduled task with no dry run. For a school-IT pilot that model is too
optimistic. Pilot machines are a mix of hardware, some with no NVIDIA GPU at
all; the installer runs unattended on accounts IT does not want to babysit; and
the reviewer signing off on the deployment needs to see, in the repo, exactly
which binaries will run.

The specific gaps: assuming CUDA breaks any machine without an NVIDIA card;
trust on first use accepts whatever the first download happens to be; there was
no side-effect-free way for an acceptance harness to exercise the install; and
uninstall left agent processes, bound ports, and the CPU thread cap behind.

## Decision

Harden the four scripts in `deploy/windows/` with surgical changes, no rewrite.

**Backend detection, never assume CUDA.** A shared helper (`lib/backend.ps1`)
probes for an NVIDIA GPU with `nvidia-smi`, falling back to WMI
(`Win32_VideoController`). `fetch-llama.ps1 -Backend auto` fetches the CUDA build
when a GPU is present and the CPU build otherwise; `-Backend cuda|cpu` overrides
the probe. On the CPU fallback the installer caps llama-server threads at half
the logical cores, clamped to at most four, through the `LLAMA_ARG_THREADS`
environment variable the pinned llama.cpp build reads. That keeps a shared
machine responsive without touching the supervisor or config schema.

**Verify against a manifest, not first use.** `llama-manifest.psd1` holds the
pinned sha256 of each asset. The fetcher verifies every download against it
before unpacking and refuses anything whose hash is missing or does not match.
Because llama.cpp publishes no per-asset checksums, the hashes are pinned once on
a trusted machine with `fetch-llama.ps1 -UpdateManifest`, reviewed in the diff,
and committed. A stock checkout ships empty placeholders and fails closed until
someone pins them.

**Idempotent install, clean uninstall.** Re-running the install drops any prior
task registration first and never clobbers a live config. Uninstall stops the
task, stops the agent and llama-server replica processes (which frees the ports
they bound), and clears the thread cap; `-Purge` also deletes `~/.fallow`. The
upgrade path is uninstall, then `fetch-llama.ps1`, then install again — the
install is safe to repeat.

**Safe PowerShell.** No `-ExecutionPolicy Bypass` anywhere. `install.ps1` and
`uninstall.ps1` support `-WhatIf` so an acceptance harness can walk the whole
path with no side effects; `install.ps1 -DryRun` still prints the rendered task
XML and exits early. The manifest loads through `Import-PowerShellDataFile`,
which reads data and runs no code.

The task XML is unchanged: it already runs at logon under the pilot account with
`InteractiveToken`, `LeastPrivilege`, and restart-on-failure, which ADR 000 and
the "why a scheduled task" note in the file explain.

## Consequences

Machines without an NVIDIA GPU now install and run on the CPU build with a
sensible thread cap. The reviewer approves the exact binaries by reviewing the
committed hashes; an altered or unexpected download is refused rather than
silently trusted. IT can dry-run the installer before a rollout and uninstall it
cleanly afterwards.

The manifest ships empty, so the first real deployment has one extra step: pin
the hashes on a trusted machine and commit them. That is on purpose. The whole
flow is still authored in a sandbox with no Windows host or network, so the
download, install, and Task Scheduler steps stay marked `(untested - verify on
target)` until someone runs them on a pilot machine.
