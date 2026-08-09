# gate discovery and static fallback

## Status

Proposed

## Date

2026-08-09

## Related

#109, #120, #121, #122

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

## Amendment: what a loopback sandbox can prove about multicast

This specification was written before #121 and #122 landed. With both merged one
clause cannot be proven as written, and it is named here rather than quietly
dropped.

The coordinator advertises through python-zeroconf, which binds the interface
addresses of the record it publishes, so a coordinator on 127.0.0.1 responds on
loopback and nowhere else. The agent queries through hashicorp/mdns, whose query
socket leaves on the host's default multicast interface. Measured on a machine
where multicast works: the loopback responder is found by a loopback browser and
is not found by the agent's query, which returns an empty answer set and no
error. Closing that gap would mean either advertising on a real LAN interface
from a test, which the loopback-only harness rule forbids, or adding a test-only
injection seam to the Go agent, which this specification excludes as production
code. So the multicast hop between our own two components is a named gap. It is
covered at each end instead of across the middle: `go-agent/discovery` covers
answer filtering, site-id matching, ordering, bounds and one query against a real
local responder, and `packages/fallow-coordinator/tests/discovery` covers the
published record, its TXT and the name-clash rename.

Everything else in the contract is proven end to end against the real vertical —
a pinned-HTTPS coordinator, a join bundle minted through the `flw` code path, and
the built Go Site runtime:

- A coordinator that moves address while keeping its site id, its certificate and
  its database is recovered by the already-enrolled agent, which finds its first
  origin dead, reaches the second through the same pinned client and resumes
  serving as the agent it already was. No second registration, no new token.
- The same profile without the mDNS opt-in probes nothing, queries nothing and
  never reaches the moved coordinator, so legacy behaviour is unchanged and
  opting in is what buys the recovery.
- A responder holding a site address under a key the profile does not pin answers
  the test and receives nothing from the agent, which skips it on the pin and
  enrolls at the coordinator with its stored pin set unchanged.
- A reachable address serves the full relayed vertical and opens no query at all,
  so an explicitly configured address wins over anything multicast could offer.
- A silent segment is reported as such, leaves the static profile and the stored
  pins byte for byte, and leaves `doctor` reading the same site id and the same
  valid pins afterwards.
- A legacy direct agent persists no site profile and starts no discovery.

Two environment notes belong on the record. A coordinator configured with
`mdns_service` opens a real loopback mDNS responder for the duration of these
scenarios, so a host that cannot open one fails this lane loudly, as a fail-loud
acceptance lane should. And the suite is the only place the real python-zeroconf
responder runs; the coordinator's own tests drive a fake.

Owned paths as implemented:

- `tests/integration/site_discovery/discovery_harness.py` (dead origin, wrong-key
  responder, the daemon's discovery log)
- `tests/integration/site_discovery/test_address_move.py`
- `tests/integration/site_discovery/test_wrong_key.py`
- `tests/integration/site_discovery/test_static_fallback.py`
- `tests/integration/site_discovery/test_legacy_mode.py`
- `tests/integration/site_discovery/{__init__,conftest}.py`
