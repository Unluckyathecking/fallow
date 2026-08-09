# add the bounded Site Mode relay

## Status

Proposed

## Date

2026-08-09

## Related

#109

## Goal

Implement the in-memory claim broker behind the HTTP relay contract without touching FastAPI or gateway composition.

## Owned paths

- `packages/fallow-coordinator/src/fallow_coordinator/site_relay/**`
- `packages/fallow-coordinator/tests/site_relay/**`
- `docs/adr/080-lan-site-relay.md`

No other path belongs to this PR. If implementation needs another existing file, stop and amend the specification before editing it.

## Contract

`RelayBroker.offer(agent_id, replica_port, request, deadline) -> RelayExchange` waits only when that agent has an available claimant. `claim(agent_id, presence_generation, timeout) -> RelayClaim | None` assigns at most one request. `start_response`, `write`, `finish` and `fail` require the same agent, claim and generation. `invalidate_agent(agent_id, newer_generation, reason)` closes queued and claimed work.

Claims use the exact JSON in `docs/lan-site/inference-claim-v1.schema.json`. Response bodies pass through a bounded async byte queue. The broker tracks whether a first response byte has crossed the retry boundary. No waiter means no queued request. Coordinator restart loses claims rather than replaying POSTs.

Limits are fixed by the contract: 2 MiB decoded request, 32 KiB response chunks, one waiter per READY slot, bounded response buffering and existing gateway deadlines.

## Verification

Deterministic async tests cover concurrent agents and slots, ownership, stale generations, duplicate completion, timeout, cancellation, backpressure, client disconnect, invalidation before and after first byte, and teardown without leaked tasks. No test opens a real network socket.

## Compatibility

LAN Site Mode remains opt-in. Existing explicit URL and Tailscale behaviour must remain unchanged unless this specification says otherwise. The implementation must pass the repository's Python or Go quality gates for every lane it touches.

## Exclusions and honest gaps

No HTTP routes, registry, gateway, app factory, protocol package or Go code. Do not add WebSockets, mTLS or a second durable queue.

School VLAN, proxy, EDR, power and reimage behaviour are not proven by sandbox tests. Any applicable item remains a named manual gate.
