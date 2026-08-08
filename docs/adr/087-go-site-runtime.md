# compose Site Mode in the Windows daemon

## Status

Proposed

## Date

2026-08-09

## Related

#109, Go pinned client PR, Go model reconciliation PR, Go claim runner PR

## Goal

Wire pinned enrollment, model reconciliation, presence ordering and inference claims into the production Go daemon.

## Owned paths

- `go-agent/config/config.go`
- `go-agent/config/config_test.go`
- `go-agent/heartbeat/client.go`
- `go-agent/heartbeat/client_test.go`
- `go-agent/heartbeat/constants.go`
- `go-agent/state/identity.go`
- `go-agent/state/identity_test.go`
- `go-agent/runtime/runtime.go`
- `go-agent/runtime/loops.go`
- `go-agent/runtime/seams.go`
- `go-agent/runtime/identity.go`
- `go-agent/runtime/runtime_test.go`
- `go-agent/cmd/agentctl/main.go`
- `docs/adr/087-go-site-runtime.md`

No other path belongs to this PR. If implementation needs another existing file, stop and amend the specification before editing it.

## Contract

Config adds optional `site_join_bundle`. Without it and without a stored site profile, all current URL, proxy and bind behaviour remains unchanged. Site Mode requires HTTPS and loopback bind.

First run parses the join file, verifies the pin, registers once, then atomically stores `agent_id`, `device_token` and the token-free site profile before removing the installed token. An ambiguous register response is not retried. Restart ignores any supplied enrollment token and uses persisted identity.

Heartbeat responses call the reconciler for every `desired_models` value, including an empty set. Heartbeats and presence events share one sequence source; events write that sequence into `detail["sequence"]`. The claim runner starts only after identity and reconciliation wiring, stops claiming immediately on user return, and shuts down before replicas. Existing suspend, eviction and idle-resume timings do not change.

`agentctl doctor -config PATH` performs read-only config, identity, llama path and pinned TLS checks and prints one JSON object. It never registers or claims work.

## Verification

Run all Go tests with race detection, vet, formatting and Windows build. Runtime tests cover legacy golden config, first enrollment, token cleanup, restart, ambiguous registration, empty assignment removal, event/heartbeat sequence ordering, active-user cancellation, shutdown order, pin/auth fatal errors and doctor exit codes.

## Compatibility

LAN Site Mode remains opt-in. Existing explicit URL and Tailscale behaviour must remain unchanged unless this specification says otherwise. The implementation must pass the repository's Python or Go quality gates for every lane it touches.

## Exclusions and honest gaps

No Python, deploy script or mDNS change. Do not change global bind validation for legacy agents.

School VLAN, proxy, EDR, power and reimage behaviour are not proven by sandbox tests. Any applicable item remains a named manual gate.
