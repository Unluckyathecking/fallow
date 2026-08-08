# install and diagnose a Windows Site Mode agent

## Status

Proposed

## Date

2026-08-09

## Related

#109, #114, #118

## Goal

Install a per-user Go Site Mode agent from one join file without leaving the enrollment token in config, logs or task arguments.

## Owned paths

- `deploy/windows/install.ps1`
- `deploy/bootstrap.ps1`
- `deploy/windows/site-join.schema.json`
- `deploy/windows/new-site-config.ps1`
- `deploy/windows/doctor.ps1`
- `deploy/windows/tests/site-mode.Tests.ps1`
- `deploy/windows/JOIN-README.md`
- `docs/adr/088-windows-site-install.md`

No other path belongs to this PR. If implementation needs another existing file, stop and amend the specification before editing it.

## Contract

`bootstrap.ps1 -JoinBundle PATH -GoBinary PATH` validates the join artifact before side effects, copies it to the protected Fallow state directory, renders TOML with `site_join_bundle` and loopback bind, and registers the existing at-logon InteractiveToken task running `agentctl run -config`. It does not copy the token into TOML or the task XML.

`doctor.ps1` combines `agentctl doctor` with Task Scheduler, ACL, process, loopback-listener and logged-in-session checks. It prints JSON keys `task_registered`, `task_running`, `interactive_session`, `config_acl`, `loopback_bind`, `llama_binary`, `spki_tls`, `identity` and exits nonzero when any required check fails. It distinguishes blocked TCP, TLS interception, pin mismatch, bad identity and no logged-in user.

After successful enrollment the installed join copy is token-free. The operator is warned that the original USB/MDM artifact remains sensitive and must be removed.

## Verification

PowerShell tests cover strict schema, escaping, ACL commands, `-WhatIf`, no-secret output, task rendering, legacy installer parity and doctor JSON. A real Windows runner verifies paths, permissions where supported, process startup/shutdown and loopback binding.

## Compatibility

LAN Site Mode remains opt-in. Existing explicit URL and Tailscale behaviour must remain unchanged unless this specification says otherwise. The implementation must pass the repository's Python or Go quality gates for every lane it touches.

## Exclusions and honest gaps

No Python agent, machine-wide service, automatic privilege elevation, mDNS or main `deploy/windows/README.md` edit. Real EDR allowlisting remains a manual school gate.

School VLAN, proxy, EDR, power and reimage behaviour are not proven by sandbox tests. Any applicable item remains a named manual gate.
