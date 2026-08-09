# Windows agent install (school-IT pilot)

Four scripts install the Fallow agent as an at-logon scheduled task in the
pilot user's session. The general deployment notes live in `deploy/README.md`;
this file covers the hardening added for the pilot. See ADR 060 for the
reasoning.

Installing a **LAN Site Mode** agent from a join file instead? Everything below
still applies — you stage llama.cpp the same way, install the same task, and
uninstall the same way. The Site Mode differences are in
[`JOIN-README.md`](JOIN-README.md), and the end-to-end operator procedure is
[`docs/lan-site/operator-runbook.md`](../../docs/lan-site/operator-runbook.md).

## Order of operations

```powershell
# 1. Pin the binary hashes on a trusted staging machine, once per backend.
#    Each run keeps the hashes the other one recorded.
deploy\windows\fetch-llama.ps1 -UpdateManifest -Backend cuda
deploy\windows\fetch-llama.ps1 -UpdateManifest -Backend cpu
# review the diff, commit llama-manifest.psd1

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
missing or altered. The manifest ships empty; pin it on a trusted machine with
`-UpdateManifest` and commit the result. A single `-UpdateManifest` run only
records the assets it fetched, so run it once with `-Backend cuda` and once with
`-Backend cpu` — otherwise a GPU staging machine leaves the `cpu` hash empty and
every CPU pilot machine refuses to fetch. A checkout with empty hashes fails
closed rather than running an unverified binary.

## Site Mode install

```powershell
deploy\bootstrap.ps1 -JoinBundle D:\join\desk-01.fallow-join -GoBinary C:\tools\agentctl.exe
```

`-JoinBundle` selects LAN Site Mode. It requires `-GoBinary`; the Python agent has
no Site Mode. `install.ps1` validates the join file before writing anything,
copies it to `%USERPROFILE%\.fallow\site\join.json` with an owner-only ACL, and
renders `agent.toml` token-free with `bind_host = "127.0.0.1"`. The agent enrolls
itself on first run and deletes its copy of the token.

Diagnose with `doctor.ps1`, which prints one JSON object covering the Scheduled
Task, the logged-in session, config and join-file ACLs, the loopback bind, the
llama binary, the stored identity, the pinned-TLS check and the clock offset
against the coordinator. It is read-only and exits non-zero when a required check
fails.

```powershell
deploy\windows\doctor.ps1
deploy\windows\doctor.ps1 -Probe     # add a live TCP/TLS reach test
```

`-Probe` is what separates a blocked port from a TLS-intercepting proxy from a pin
mismatch. On Windows PowerShell 5.1 it cannot compute the presented public-key
hash, so it reports reachability only and leaves `agentctl` as the pin authority;
run it from `pwsh` 7+ where you can.

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

For a Site Mode teardown use `-Purge`. Without it the enrolled site identity
survives and the machine rejoins the site as the same agent at the next login.
