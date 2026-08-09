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
- `go-agent/runtime/ordering_test.go` (new)
- `go-agent/runtime/fixes_test.go` (new)
- `go-agent/runtime/eventsink.go`
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

Heartbeat responses hand the reconciler the latest `desired_models` value, including an empty set, on its own worker that coalesces to the newest set so a slow model download never stalls the heartbeat loop. Reconciliation is gated on serving-eligibility: it never starts a replica while the user is active or the machine is reclaimed (VRAM eviction happens while active, so idle covers it too), and it applies the deferred set once the machine is idle again. Because the eligibility check and a slow model download are not atomic, the reconciler's supervisor is additionally wrapped in a guard that re-checks eligibility immediately before every start, so a user who returns during the download cannot cause a replica start after suspension. The claim runner starts only after identity and reconciliation wiring, stops claiming immediately on user return, and shuts down before replicas. A routine relay fencing response (410/409/404 on a response upload or failure report) terminates that claim and keeps polling; it is not a fatal error. An unexpected claim-runner exit stops serving but does not take down the daemon — a genuine auth rejection is surfaced fatally by the heartbeat loop, which shares the device token. Existing suspend, eviction and idle-resume timings do not change.

Heartbeats and presence events share one monotonic sequence. Direct (legacy) agents keep a volatile per-process source that resets on restart, unchanged. Site Mode agents draw from a source persisted in the identity file as a reserved high-water mark: it writes the reserved ceiling to disk before advancing its in-memory ceiling, so no value is handed out past what is durable and a fresh process resumes at or above the last value a predecessor could have used — the coordinator's presence fence (#112) never regresses across a restart. The sequencing sink stamps each presence event's sequence into `detail["sequence"]` and, at a user-return transition, cancels the in-flight claim before the event reaches the wire. The poll loop samples presence and drives the state machine before it exposes READY availability, so a claim is never offered on the first startup tick while the user is actually active. Reconciliation is held ineligible until that first authoritative presence and reclaim sample completes, so a heartbeat arriving before the first poll on restart cannot act on the constructor defaults (idle, unreclaimed) and start a download or replica for an active or reclaimed user; the deferred set applies on the first eligible tick. Presence events and heartbeats are ordered on the wire: the heartbeat allocates its sequence under the same lock the sink stamps under and then flushes queued events, so a higher-sequence heartbeat can never overtake and orphan a queued lower-sequence event at the coordinator's monotonic fence. A presence event that fails to deliver is retried and, if still undeliverable, fails the daemon closed rather than acknowledging the flush and letting a heartbeat overtake the lost transition. Reclaim runs outside the preemption state machine, so its release publishes a sequenced `user_idle` event to advance the durable presence generation back up to the fence the serving-paused heartbeats raised; without it, serving would stay fenced after release. A persisted Site profile is validated to carry a usable https coordinator origin before it is dialed, failing closed on corrupt stored state instead of indexing an empty list.

`agentctl doctor -config PATH` performs read-only config, identity, llama path and pinned TLS checks and prints one JSON object. It never registers or claims work: it builds the pinned client to validate the pin set statically but opens no coordinator connection, and exits non-zero when a required check fails.

## Verification

Run all Go tests with race detection, vet, formatting, the schema drift check and the Windows build. Coverage: legacy golden config and disabled-mode parity (no Site seam is touched without opt-in); Site config validation and loopback-only bind; the token-free profile and high-water sequence round-trip beside a still-loading legacy identity; first enrollment with token removal; ambiguous and errored registration failing closed without retry; restart resuming from the persisted profile without re-registering and continuing above the high-water mark; the persisted sequence keeping the handout bounded to what is durable on a write failure; reconciliation gated on serving-eligibility (deferred while active/reclaimed and until the first authoritative presence/reclaim sample on restart, applied on the eligibility edge, coalescing to the latest set); the claim runner starting only when eligible and stopping before replicas; a relay fencing status terminating a claim rather than the daemon; the event-sink flush barrier and the heartbeat sequence never overtaking a queued presence event; the reclaim release publishing a sequenced user_idle event; availability generation advancing and cancelling a claim on user return; loopback-only replica targeting; and the relay-v1 client driven against a server that mirrors the merged coordinator routes (claim 200/204, response fencing headers and 202/410/409/404, typed failure body).

## Compatibility

LAN Site Mode remains opt-in. Existing explicit URL and Tailscale behaviour must remain unchanged unless this specification says otherwise. The implementation must pass the repository's Python or Go quality gates for every lane it touches.

## Exclusions and honest gaps

No Python, deploy script or mDNS change. Global bind validation for legacy agents is untouched; the loopback requirement applies only to Site Mode.

The relay-v1 server routes merged in #113, so the claim client is written and tested against the real merged wire (paths, fencing headers, status codes and the strict claim/failure JSON) via a mirror server. What sandbox tests do not prove is a live end-to-end exchange against a running coordinator process; that cross-process integration and failover across multiple pinned coordinator URLs (the client dials the first) remain named integration gates.

School VLAN, proxy, EDR, power and reimage behaviour are not proven by sandbox tests. In particular, IT must still confirm the identity and token-free profile survive reboot and any reimaging product before the pilot. Any applicable item remains a named manual gate.
