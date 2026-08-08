# resolve optional Site Mode mDNS candidates

## Status

Proposed

## Date

2026-08-09

## Related

#109, #115, #118, #120

## Goal

Allow a site profile to try mDNS candidates without learning trust from multicast.

## Owned paths

- `go-agent/discovery/**`
- `go-agent/config/config.go`
- `go-agent/config/config_test.go`
- `go-agent/runtime/runtime.go`
- `go-agent/runtime/runtime_test.go`
- `go-agent/go.mod`
- `go-agent/go.sum`
- `docs/adr/091-lan-site-go-mdns.md`

No other path belongs to this PR. If implementation needs another existing file, stop and amend the specification before editing it.

## Contract

Static coordinator URLs remain first and sufficient. If they are unreachable and `mdns_service` is present, the resolver performs a bounded `_fallow._tcp.local.` query, filters matching `site_id`, sorts candidates deterministically and hands them to the existing pinned client. Every candidate must pass the stored SPKI pin before any secret is sent.

Discovery timeout returns a typed diagnostic and leaves the static profile intact. Pin mismatch skips that candidate but never changes the pin set. Multicast loss is normal and does not cause HTTP, public DNS or subnet-scan fallback.

## Verification

Tests use an injected resolver for duplicate, malformed, hostile, IPv4/IPv6 and timeout cases. An integration test uses a local responder and wrong certificate. Run module licensing checks, `go mod verify`, race tests and Windows build.

## Compatibility

LAN Site Mode remains opt-in. Existing explicit URL and Tailscale behaviour must remain unchanged unless this specification says otherwise. The implementation must pass the repository's Python or Go quality gates for every lane it touches.

## Exclusions and honest gaps

No coordinator advertiser, token in TXT, TOFU, broadcast scan or direct replica change. This PR is serialized after Go runtime composition because it owns config/runtime and dependency files.

School VLAN, proxy, EDR, power and reimage behaviour are not proven by sandbox tests. Any applicable item remains a named manual gate.
