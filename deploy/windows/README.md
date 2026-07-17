# Windows agent install (school-IT pilot)

Four scripts install the Fallow agent as an at-logon scheduled task in the
pilot user's session. The general deployment notes live in `deploy/README.md`;
this file covers the hardening added for the pilot. See ADR 060 for the
reasoning.

## Order of operations

```powershell
# 1. Pin the binary hashes once, on a trusted staging machine.
deploy\windows\fetch-llama.ps1 -UpdateManifest   # review the diff, commit llama-manifest.psd1

# 2. On each pilot machine: stage the right build, then install.
deploy\windows\fetch-llama.ps1                    # auto-detects NVIDIA vs CPU, verifies hashes
deploy\windows\install.ps1                        # registers and starts the task
```

## Backend detection

`fetch-llama.ps1` picks the llama.cpp build to match the machine. `-Backend
auto` (the default) fetches the CUDA build when it finds an NVIDIA GPU and the
CPU build otherwise. Pass `-Backend cuda` or `-Backend cpu` to override. On the
CPU fallback, `install.ps1` caps `LLAMA_ARG_THREADS` for the pilot account so
the CPU build does not saturate a shared machine.

## Binary verification

`llama-manifest.psd1` holds the pinned sha256 of each asset. `fetch-llama.ps1`
verifies every download against it before unpacking and refuses anything that is
missing or altered. The manifest ships empty; pin it once with `-UpdateManifest`
on a trusted machine and commit the result. A checkout with empty hashes fails
closed rather than running an unverified binary.

## Dry runs

- `install.ps1 -WhatIf` and `uninstall.ps1 -WhatIf` walk the whole path and
  change nothing. The acceptance harness uses this.
- `install.ps1 -DryRun` prints the rendered task XML and exits before touching
  the system.

## Upgrade and uninstall

Upgrading is uninstall, re-fetch, install — the install is safe to repeat and
never clobbers a live `~/.fallow\agent.toml`.

```powershell
deploy\windows\uninstall.ps1          # stop task + processes, free ports, keep ~\.fallow
deploy\windows\uninstall.ps1 -Purge   # also delete ~\.fallow (config, models, logs)
```
