# publish the LAN Site Mode pilot runbook

## Status

Proposed

## Date

2026-08-09

## Related

#109, #120, #123

## Goal

Publish the tested operator path and reconcile the shared documentation after all implementation PRs merge.

## Owned paths

- `docs/lan-site/operator-runbook.md`
- `docs/quickstart.md`
- `docs/pilot/it-checklist.md`
- `docs/pilot/admin-runbook.md`
- `docs/pilot/data-policy-signoff.md`
- `docs/compatibility.md`
- `deploy/README.md`
- `deploy/windows/README.md`
- `SECURITY.md`
- `docs/adr/README.md`
- `CHANGELOG.md`

No other path belongs to this PR. If implementation needs another existing file, stop and amend the specification before editing it.

## Contract

The runbook covers one stable coordinator address, certificate and pin preparation, four join files, Windows install, `doctor`, model assignment, live availability, active-user preemption, restart, revocation, rollback and removal. Commands must match the merged CLI and scripts.

The IT checklist contains exact desktop-to-coordinator firewall needs, proxy/TLS-inspection exception, EDR allowlisting, logged-in account, sleep policy and persistent-state confirmation. The acceptance table separates automated evidence from school-only checks and names expected output for each manual command.

Security documentation states that legacy mode still relies on Tailscale while Site Mode uses pinned HTTPS and loopback replicas. The ADR index and changelog receive one short reconciled entry.

## Verification

Check links and command help against the merged tree. Run documentation examples that the acceptance harness can exercise. Apply `/humanizer` to every changed prose file and keep unverified school steps labelled manual.

## Compatibility

LAN Site Mode remains opt-in. Existing explicit URL and Tailscale behaviour must remain unchanged unless this specification says otherwise. The implementation must pass the repository's Python or Go quality gates for every lane it touches.

## Exclusions and honest gaps

No feature code or new behaviour. This is the only LAN PR allowed to edit the shared append files and main deployment documentation.

School VLAN, proxy, EDR, power and reimage behaviour are not proven by sandbox tests. Any applicable item remains a named manual gate.
