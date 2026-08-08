# gate discovery and static fallback

## Status

Proposed

## Date

2026-08-09

## Related

#109, static acceptance PR, coordinator mDNS PR, Go mDNS PR

## Goal

Prove that optional discovery improves address recovery without becoming a trust or availability dependency.

## Owned paths

- `tests/integration/site_discovery/**`
- `docs/adr/092-lan-site-discovery-acceptance.md`

No other path belongs to this PR. If implementation needs another existing file, stop and amend the specification before editing it.

## Contract

The harness moves the coordinator to a new address, advertises the same site ID and presents the pinned key. The agent rediscovers and reconnects without enrollment. A responder with the right site ID and wrong key sees no HTTP request. With multicast blocked, the original static address remains healthy and diagnostics report discovery unavailable without changing trust.

Explicit/manual site address wins over cache and mDNS. Existing legacy explicit URL mode does not start discovery.

## Verification

Run bounded local multicast tests where supported and deterministic injected tests everywhere. Cover DHCP-style address change, stale cache, duplicate responders, wrong site, wrong pin, blocked multicast and legacy precedence. Record platform skips as honest gaps; the real school VLAN remains manual.

## Compatibility

LAN Site Mode remains opt-in. Existing explicit URL and Tailscale behaviour must remain unchanged unless this specification says otherwise. The implementation must pass the repository's Python or Go quality gates for every lane it touches.

## Exclusions and honest gaps

No production code, workflow YAML, shared changelog or ADR index.

School VLAN, proxy, EDR, power and reimage behaviour are not proven by sandbox tests. Any applicable item remains a named manual gate.
