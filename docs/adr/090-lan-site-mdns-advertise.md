# optionally advertise the Site Mode address

## Status

Proposed

## Date

2026-08-09

## Related

#109, #113, #120

## Goal

Add optional mDNS advertisement after the static Site Mode path is proven.

## Owned paths

- `packages/fallow-coordinator/src/fallow_coordinator/discovery/**`
- `packages/fallow-coordinator/tests/discovery/**`
- `packages/fallow-coordinator/src/fallow_coordinator/app/factory.py`
- `packages/fallow-coordinator/pyproject.toml`
- `uv.lock`
- `docs/adr/090-lan-site-mdns-advertise.md`

No other path belongs to this PR. If implementation needs another existing file, stop and amend the specification before editing it.

## Contract

When `[site].mdns_service` is enabled, coordinator lifespan advertises `_fallow._tcp.local.` on the configured site interface. TXT contains only `version=1` and `site_id`. SRV/A/AAAA data supplies the configured HTTPS address and port. Disabled config creates no socket or background task.

Use one reviewed cross-platform mDNS dependency rather than handwritten DNS packets. Startup fails clearly on an invalid or ambiguous interface; shutdown unregisters the service. Advertisement is an address hint and carries no pin, token, model or credential.

## Verification

Tests inject the advertiser and cover register/unregister, interface choice, duplicate names, shutdown, disabled mode and secret absence. `uv lock --check`, licensing review and the full Python quality gate are required.

## Compatibility

LAN Site Mode remains opt-in. Existing explicit URL and Tailscale behaviour must remain unchanged unless this specification says otherwise. The implementation must pass the repository's Python or Go quality gates for every lane it touches.

## Exclusions and honest gaps

No Go resolver, installer, trust change or static-path fallback change. This PR intentionally follows the coordinator composition PR because both own `app/factory.py`.

School VLAN, proxy, EDR, power and reimage behaviour are not proven by sandbox tests. Any applicable item remains a named manual gate.
