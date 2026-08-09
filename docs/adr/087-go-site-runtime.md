# compose Site Mode in the Windows daemon

## Status

Proposed

## Date

2026-08-09

## Related

#109, #113, #115, #116, #117

## Goal

Wire pinned enrollment, model reconciliation, presence ordering and inference claims into the production Go daemon.

## Owned paths

- `go-agent/config/config.go`
- `go-agent/config/config_test.go`
- `go-agent/state/identity.go`
- `go-agent/state/identity_test.go`
- `go-agent/runtime/runtime.go`
- `go-agent/runtime/loops.go`
- `go-agent/runtime/seams.go`
- `go-agent/runtime/identity.go`
- `go-agent/runtime/site.go` (new)
- `go-agent/runtime/sequence.go` (new)
- `go-agent/runtime/availability.go` (new)
- `go-agent/runtime/relay.go` (new)
- `go-agent/runtime/sitesink.go` (new)
- `go-agent/runtime/runtime_test.go`
- `go-agent/runtime/site_test.go` (new)
- `go-agent/runtime/sequence_test.go` (new)
- `go-agent/runtime/availability_test.go` (new)
- `go-agent/runtime/relay_test.go` (new)
- `go-agent/cmd/agentctl/main.go`
- `docs/adr/087-go-site-runtime.md`

The Site Mode composition split into several small runtime files rather than one
large one, matching the operating standard's 200-400 line target and one
responsibility per module. The specification's original single-file plan named
`go-agent/heartbeat/client.go`, `heartbeat/client_test.go` and
`heartbeat/constants.go`; composing over the existing pinned Doer seam needed no
change to the heartbeat client, so those files are untouched.

No other path belongs to this PR. If implementation needs another existing file, stop and amend the specification before editing it.

## Contract

Config adds optional `site_join_bundle`. Without it and without a stored site profile, all current URL, proxy and bind behaviour remains unchanged. Site Mode requires HTTPS and loopback bind.

First run parses the join file, verifies the pin, registers once, then atomically stores `agent_id`, `device_token` and the token-free site profile before removing the installed token. An ambiguous register response is not retried. Restart ignores any supplied enrollment token and uses persisted identity.

Heartbeat responses call the reconciler for every `desired_models` value, including an empty set. Reconciliation runs on its own worker, coalescing to the newest desired set, so a slow model download never stalls the heartbeat loop. The claim runner starts only after identity and reconciliation wiring, stops claiming immediately on user return, and shuts down before replicas. Existing suspend, eviction and idle-resume timings do not change.

Heartbeats and presence events share one monotonic sequence. Direct (legacy) agents keep a volatile per-process source that resets on restart, unchanged. Site Mode agents draw from a source persisted in the identity file as a reserved high-water mark: it writes the reserved ceiling to disk before handing any value out, so a fresh process resumes at or above the last value a predecessor could have used and the coordinator's presence fence (#112) never regresses across a restart. The sequencing sink stamps each presence event's sequence into `detail["sequence"]` and, at a user-return transition, cancels the in-flight claim before the event reaches the wire, holding the relay-v1 local order (suspend, cancel claim, then send the event).

`agentctl doctor -config PATH` performs read-only config, identity, llama path and pinned TLS checks and prints one JSON object. It never registers or claims work: it builds the pinned client to validate the pin set statically but opens no coordinator connection, and exits non-zero when a required check fails.

## Verification

Run all Go tests with race detection, vet, formatting, the schema drift check and the Windows build. Coverage: legacy golden config and disabled-mode parity (no Site seam is touched without opt-in); Site config validation and loopback-only bind; the token-free profile and high-water sequence round-trip beside a still-loading legacy identity; first enrollment with token removal; ambiguous and errored registration failing closed without retry; restart resuming from the persisted profile without re-registering and continuing above the high-water mark; reconciliation of every desired set including empty; the claim runner starting only when eligible and stopping before replicas; availability generation advancing and cancelling a claim on user return; loopback-only replica targeting; and the relay-v1 client driven against a server that mirrors the merged coordinator routes (claim 200/204, response fencing headers and 202/410, typed failure body).

## Compatibility

LAN Site Mode remains opt-in. Existing explicit URL and Tailscale behaviour must remain unchanged unless this specification says otherwise. The implementation must pass the repository's Python or Go quality gates for every lane it touches.

## Exclusions and honest gaps

No Python, deploy script or mDNS change. Global bind validation for legacy agents is untouched; the loopback requirement applies only to Site Mode.

The relay-v1 server routes merged in #113, so the claim client is written and tested against the real merged wire (paths, fencing headers, status codes and the strict claim/failure JSON) via a mirror server. What sandbox tests do not prove is a live end-to-end exchange against a running coordinator process; that cross-process integration and failover across multiple pinned coordinator URLs (the client dials the first) remain named integration gates.

School VLAN, proxy, EDR, power and reimage behaviour are not proven by sandbox tests. In particular, IT must still confirm the identity and token-free profile survive reboot and any reimaging product before the pilot. Any applicable item remains a named manual gate.
