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

## What the runbook carries from the rest of the wave

Three findings from earlier PRs in this wave are load-bearing in the operator
path and are recorded here so they cannot be edited out by accident.

The live multicast hop is a named gap in ADR 092: the advertiser answers on the
interface it advertises and the resolver queries the default multicast interface,
so a loopback harness cannot join them. The runbook carries that gap's concrete
closer as a pilot-day check — browse `_fallow._tcp.local.` from an agent machine
on the school LAN and confirm the `site_id` TXT value against the join files
before relying on discovery for anything. Static addressing, by DHCP reservation
or internal DNS, stays the baseline; mDNS is optional recovery.

`agentctl doctor` gained a clock lane (ADR 095). A clock far enough out that
certificates fall outside their validity window cannot be measured at all,
because the handshake fails before a `Date` header is served, and doctor names
the PC's clock as the likely cause instead of the certificate. That produces two
obligations the runbook meets: an NTP and date/time prerequisite in the IT
checklist, and a troubleshooting row mapping "certificate outside validity
window" to date, time zone and NTP sync.

`flw site status` (ADR 094) is the pilot-day pane, and one of its behaviours reads
as a fault until it is explained. A machine that is wiped and re-enrolled from a
new join file returns as a new agent id, and its old identity remains a permanent
`offline` row with an unbounded heartbeat age, because no route deletes an agent
record. The runbook states that this is expected and that the live identity is the
row with a fresh age.

## Owned paths as implemented

- `docs/lan-site/operator-runbook.md` (new)
- `docs/quickstart.md`, `docs/compatibility.md`
- `docs/pilot/it-checklist.md`, `docs/pilot/admin-runbook.md`,
  `docs/pilot/data-policy-signoff.md`
- `deploy/README.md`, `deploy/windows/README.md`
- `SECURITY.md`, `docs/adr/README.md`, `CHANGELOG.md`
- `docs/adr/093-lan-site-pilot-runbook.md`
