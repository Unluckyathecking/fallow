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

Plus the runtime files amended under "Amendment: the runtime call site after #118" below.

Beyond those, no other path belongs to this PR. If implementation needs another existing file, stop and amend this specification before editing it.

## Contract

Static coordinator URLs remain first and sufficient. If they are unreachable and `mdns_service` is present, the resolver performs a bounded `_fallow._tcp.local.` query, filters matching `site_id`, sorts candidates deterministically and hands them to the existing pinned client. The query goes through `github.com/hashicorp/mdns`, whose one-shot bounded lookup matches this contract without browse machinery. Every candidate must pass the stored SPKI pin before any secret is sent.

An answer advertises its site in a single TXT `site=<site_id>` field, matched byte for byte against the profile. The field is a public routing label, not a credential: it only discards answers meant for another site, and confers no trust. An answer carrying no site field, more than one, or a name outside the service is discarded rather than guessed at.

Candidates are built from the numeric addresses in the answer, never from the advertised host name, so resolving a candidate cannot fall through to public DNS. Ordering is IPv4 before IPv6, then by address, then by port, so two agents on one segment dial the same candidate first regardless of the order answers arrived in. The count is capped so a hostile responder cannot turn a bounded fallback into a long sequence of dials.

Discovery timeout returns a typed diagnostic and leaves the static profile intact. Pin mismatch skips that candidate but never changes the pin set. Multicast loss is normal and does not cause HTTP, public DNS or subnet-scan fallback.

The dependency is pinned at `github.com/hashicorp/mdns v1.0.6`, which leaves the module's Go directive at 1.23.0. v1.0.7 fixes a real race — given a cancellable context the library's two teardown paths, the watcher it starts for `ctx.Done` and the deferred close at the end of the query, both call `client.Close`, whose log statement reads the whole client struct while the other path compare-and-swaps a field inside it — but it declares `go 1.25`, and a toolchain move that carries both Go workflows with it is a chore of its own rather than part of a feature. The race is avoided at the call instead: the query is given a context stripped of cancellation, so only one teardown path ever runs, and the caller's cancellation is expressed as the query bound. A cancelled query therefore returns at its bound rather than at once, which is bounded by construction and only ever delays startup wiring.

## Verification

Tests use an injected resolver for duplicate, malformed, hostile, IPv4/IPv6 and timeout cases. An integration test uses a local responder and wrong certificate. Run module licensing checks, `go mod verify`, race tests and Windows build.

## Compatibility

LAN Site Mode remains opt-in. Existing explicit URL and Tailscale behaviour must remain unchanged unless this specification says otherwise. The implementation must pass the repository's Python or Go quality gates for every lane it touches.

## Exclusions and honest gaps

No coordinator advertiser, token in TXT, TOFU, broadcast scan or direct replica change. This PR is serialized after Go runtime composition because it owns config/runtime and dependency files.

Config gains no new key: the contract asks for no tunable, so `config.go` is untouched.

The IPv6-only segment is a named gap. A dual-stack query aborts on a host with no IPv6 route, so the lookup retries once over IPv4 while still returning IPv6 addresses found in the answers; a segment reachable only over IPv6 multicast is not covered. The licensing check is run by hand — `github.com/hashicorp/mdns` is MIT and `github.com/miekg/dns` and `golang.org/x/net` are BSD-3-Clause, all compatible with this repository's AGPL-3.0 — because `.github/**` is outside the owned paths and no licensing lane exists to extend.

School VLAN, proxy, EDR, power and reimage behaviour are not proven by sandbox tests. Any applicable item remains a named manual gate.

## Amendment: the runtime call site after #118

This specification's owned paths name `go-agent/runtime/runtime.go`, which was
where the daemon's composition lived when it was written. #118 then split Site
Mode out of that file: `resolveSite` and `enrollSite` now live in
`go-agent/runtime/site.go` and choose the coordinator origin through
`firstCoordinatorURL`, and the injectable collaborators live in the `Seams`
struct in `go-agent/runtime/seams.go`. `runtime.go` holds only the loop
composition and carries no coordinator-URL logic, so the resolver has no call
site inside the paths as originally listed.

Additional owned paths for this amendment:

- `go-agent/runtime/seams.go` (the `Discovery` seam)
- `go-agent/runtime/site.go` (the base URL choice)
- `go-agent/runtime/site_test.go` (the existing coverage for both)

`go-agent/runtime/doctor.go`, `go-agent/runtime/doctor_test.go` and
`go-agent/cmd/agentctl/main.go` remain outside this PR and are untouched.

Wiring. `Seams` gains `Discovery siteclient.Discovery`, defaulting to the mDNS
resolver, so the fallback is injectable wherever the rest of Site Mode already
is. `siteBaseURL` chooses the origin to dial: a profile without `mdns_service`
takes its first static origin with no extra network call at all, so an agent
that never opted in behaves exactly as before; a profile with `mdns_service`
probes its static origins in listed order through the pinned client and only
their unreachability opens the query. The probe is an unauthenticated GET whose
only question is whether a pinned peer answers, so no credential travels before
the pin has been checked, and a candidate that cannot present a stored pin is
skipped like any other unusable origin.

Availability is never narrowed by the fallback. A query that times out, fails or
yields nothing usable leaves the static profile in place and startup proceeds, so
a silent segment costs a bounded delay rather than a daemon that will not start.
A first run reuses the origin it enrolled against instead of resolving a second
time, which also prevents enrolling against one coordinator and then dialing
another.

Probing all static origins rather than only the first also closes, for the mDNS
path, the multi-URL failover that ADR 087 left as a named integration gate.
Nothing changes for a profile without `mdns_service`, which still dials its first
static origin unconditionally.
