# serve Site Mode claims from loopback

## Status

Proposed

## Date

2026-08-09

## Related

#109, #115

## Goal

Implement the standalone Go claim loop that forwards bounded Site Mode work to a READY loopback replica.

## Owned paths

- `go-agent/inference/**`
- `docs/adr/086-go-site-claim-runner.md`

No other path belongs to this PR. If implementation needs another existing file, stop and amend the specification before editing it.

## Contract

`Runner.Run(ctx, Availability, ReplicaTarget)` holds one claim per available slot only while the shared presence controller is IDLE and reclaim is false. It decodes the v1 claim body, verifies the port belongs to a READY local replica, and sends the allowed POST to `127.0.0.1`. Its local HTTP transport has `Proxy=nil` and cannot follow redirects.

The runner accepts injected `AvailabilitySource.Snapshot() AvailabilitySnapshot` (with `Ready`, `Generation` and a `Changed` channel), `ReplicaTarget.ReadyLoopbackPort(port) bool`, and `Coordinator` seams. It claims with a bounded 25-second wait; `Changed` cancels an outstanding claim and local request/upload. The runner uploads the upstream status, content type, presence generation and raw response stream. On user return or reclaim it cancels both local request and upload, then reports an allowed failure code when possible. It never forwards client Authorization. Claim, request, response and concurrency bounds follow `relay-v1.md`.

## Verification

Tests cover buffered JSON, raw SSE bytes, wrong ports and paths, response status, active-before-start, active-midstream, reclaim, slow readers, coordinator disconnect, 204 claim timeout, auth failure and proxy traps. Verify no goroutine leaks and run `go test -race`.

## Compatibility

LAN Site Mode remains opt-in. Existing explicit URL and Tailscale behaviour must remain unchanged unless this specification says otherwise. The implementation must pass the repository's Python or Go quality gates for every lane it touches.

## Exclusions and honest gaps

No runtime wiring, model reconciliation, TLS implementation, WebSocket, mDNS or coordinator code.

School VLAN, proxy, EDR, power and reimage behaviour are not proven by sandbox tests. Any applicable item remains a named manual gate.
