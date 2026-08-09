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

Static coordinator URLs remain first and sufficient. If they are unreachable and `mdns_service` is present, the resolver performs a bounded `_fallow._tcp.local.` query, filters matching `site_id`, sorts candidates deterministically and hands them to the existing pinned client. The query goes through `github.com/hashicorp/mdns`, whose one-shot bounded lookup matches this contract without browse machinery. Every candidate must pass the stored SPKI pin before any secret is sent.

An answer advertises its site in a single TXT `site=<site_id>` field, matched byte for byte against the profile. The field is a public routing label, not a credential: it only discards answers meant for another site, and confers no trust. An answer carrying no site field, more than one, or a name outside the service is discarded rather than guessed at.

Candidates are built from the numeric addresses in the answer, never from the advertised host name, so resolving a candidate cannot fall through to public DNS. Ordering is IPv4 before IPv6, then by address, then by port, so two agents on one segment dial the same candidate first regardless of the order answers arrived in. The count is capped so a hostile responder cannot turn a bounded fallback into a long sequence of dials.

Discovery timeout returns a typed diagnostic and leaves the static profile intact. Pin mismatch skips that candidate but never changes the pin set. Multicast loss is normal and does not cause HTTP, public DNS or subnet-scan fallback.

The dependency is pinned at `github.com/hashicorp/mdns v1.0.7`. Earlier releases race their own two close paths when a query's context is cancelled, which `go test -race` catches and a daemon shutdown would hit. That release declares `go 1.25`, so the module's Go directive moves from 1.23.0 to 1.25 with it; both Go workflows read the toolchain from `go-agent/go.mod` and follow.

## Verification

Tests use an injected resolver for duplicate, malformed, hostile, IPv4/IPv6 and timeout cases. An integration test uses a local responder and wrong certificate. Run module licensing checks, `go mod verify`, race tests and Windows build.

## Compatibility

LAN Site Mode remains opt-in. Existing explicit URL and Tailscale behaviour must remain unchanged unless this specification says otherwise. The implementation must pass the repository's Python or Go quality gates for every lane it touches.

## Exclusions and honest gaps

No coordinator advertiser, token in TXT, TOFU, broadcast scan or direct replica change. This PR is serialized after Go runtime composition because it owns config/runtime and dependency files.

The resolver is not yet called by the daemon. This specification's owned paths name `runtime/runtime.go`, but #118 moved the whole Site Mode composition into `runtime/site.go` and `runtime/seams.go`: `resolveSite` and `enrollSite` choose the base URL through `firstCoordinatorURL`, and the injectable collaborators are the `Seams` struct. Neither file is owned here, so the call site — a `Discovery` seam and a static-candidates-then-discovery base URL choice — stays out of this PR rather than being edited outside the specification. Config gains no new key: the contract asks for no tunable, so `config.go` is untouched.

The IPv6-only segment is a named gap. A dual-stack query aborts on a host with no IPv6 route, so the lookup retries once over IPv4 while still returning IPv6 addresses found in the answers; a segment reachable only over IPv6 multicast is not covered. The licensing check is run by hand — `github.com/hashicorp/mdns` is MIT and `github.com/miekg/dns` and `golang.org/x/net` are BSD-3-Clause, all compatible with this repository's AGPL-3.0 — because `.github/**` is outside the owned paths and no licensing lane exists to extend.

School VLAN, proxy, EDR, power and reimage behaviour are not proven by sandbox tests. Any applicable item remains a named manual gate.
