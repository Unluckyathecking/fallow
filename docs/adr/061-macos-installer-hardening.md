# ADR 061: Hardening the macOS agent installer for a school-IT pilot

**Status:** Accepted

**Date:** 2026-07-17

## Context

The macOS installer under `deploy/macos/` was written to prove the launchd
wiring, not to be handed to a school IT team. Three gaps mattered for the pilot.

It assumed Apple Silicon and left backend choice implicit. A machine on Intel
would get no working llama-server and no guard rail on how many cores the agent
grabs on a shared classroom Mac.

Nothing checked the binaries before launchd ran them. The LaunchAgent was wired
to whatever sat at the expected path, so a swapped or truncated download would
be executed on next login.

`uninstall.sh` removed the LaunchAgent and the plist but left orphaned replicas
alone. A crashed agent could leave llama-server processes holding ports in the
replica range, and a clean-looking uninstall would still leave them bound.

## Decision

Keep the two-flavour design (Python venv, prebuilt Go binary) and the
`FALLOW_INSTALL_DRY_RUN` render seam. Add four things around them.

Backend detection: `install.sh` reads `uname -m`. Apple Silicon selects the
Metal build at `deploy/bin/macos/llama-server`; Intel selects a CPU build at
`deploy/bin/macos-x64/llama-server` and caps `OMP_NUM_THREADS` at half the cores
(floor 1) so the agent leaves headroom on a shared machine. The choice is
written into the LaunchAgent as `FALLOW_LLAMA_SERVER_BINARY`, which the agent and
its child llama-server already read. `FALLOW_INSTALL_BACKEND=metal|cpu` overrides
the detected arch so the render test can exercise both paths on one host.

Verify before execution: a new `verify-sha256.sh` checks a file's SHA256
against a signed manifest (`manifest.sha256`, format shown in
`manifest.sha256.example`). `install.sh` runs it on every binary it is about to
wire — the Go agent binary before it is copied, the llama-server binary before
launchd loads the agent — and fails closed on a mismatch or a missing entry. The
verifier is its own POSIX `sh` module so the render test can drive it directly
with throwaway fixtures.

Clean uninstall: `uninstall.sh` still boots the LaunchAgent out and removes
the plist, then reads the replica port range from the installed config (env
override wins, shipped default as fallback) and terminates whatever still listens
on those ports. `--purge` removes `~/.fallow` state as before.

Upgrade path: bump the pinned llama.cpp release, re-run `fetch-llama.sh`,
update `manifest.sha256`, then re-run `install.sh`. The install is idempotent: it
boots out the old LaunchAgent and reloads the new one, keeping the live config.

The LaunchAgent stays a per-user agent bootstrapped into `gui/$UID`, so it runs
as the pilot account with no privilege escalation. `KeepAlive` plus
`ThrottleInterval` keep the restart-on-failure behaviour and throttle crash
storms. That wiring is unchanged; ADR 040 explains why it must be an agent and
not a daemon.

## Consequences

The installer picks the right backend on both Mac architectures and refuses to
launch an unverified binary. Uninstall leaves no replica holding a port.

The manifest has to be produced and signed per release; an unfilled or absent
`manifest.sha256` blocks the install by design. The Intel CPU build is selected
but not yet fetched — `fetch-llama.sh` still ships the arm64 asset only, so an
Intel host is wired and warned until that build exists.

`install.sh` and `uninstall.sh` stay bash for safe argv arrays; the new verifier
is POSIX `sh`. All three pass shellcheck. Scripts were authored in a sandbox, so
the launchctl, uv, and download steps remain marked untested on target.
