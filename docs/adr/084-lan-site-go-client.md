# add strict join files and pinned HTTPS

## Status

Proposed

## Date

2026-08-09

## Related

#109

## Goal

Add the standalone Go trust boundary for parsing a join file, resolving static candidates and dialing the coordinator directly.

## Owned paths

- `go-agent/siteclient/**`
- `docs/adr/084-lan-site-go-client.md`

No other path belongs to this PR. If implementation needs another existing file, stop and amend the specification before editing it.

## Contract

`ParseJoin([]byte) (JoinBundle, error)` rejects unknown fields and follows `join-bundle-v1.schema.json`. `Profile()` removes the enrollment token. `Resolver.Candidates(ctx, profile)` returns static HTTPS origins in listed order; discovery is an injected optional interface and has no implementation here.

`NewPinnedClient(profile)` returns an `http.Client` whose transport has `Proxy=nil`, redirects disabled and `tls.Config.VerifyConnection` checking certificate time and constant-time SPKI membership on every connection. It exposes typed config, pin, auth and transient errors. No Authorization header or body is written before verification succeeds.

## Verification

Tests use local TLS servers and a proxy trap. Cover matching and wrong pins, next-pin acceptance, expired certificates, HTTP URL rejection, redirects, candidate ordering, unknown fields, token redaction and Windows compilation. The wrong-pin server must observe no HTTP request.

## Compatibility

LAN Site Mode remains opt-in. Existing explicit URL and Tailscale behaviour must remain unchanged unless this specification says otherwise. The implementation must pass the repository's Python or Go quality gates for every lane it touches.

## Exclusions and honest gaps

No runtime/config wiring, enrollment call, mDNS dependency, model cache or coordinator code. Use the standard library only.

School VLAN, proxy, EDR, power and reimage behaviour are not proven by sandbox tests. Any applicable item remains a named manual gate.
