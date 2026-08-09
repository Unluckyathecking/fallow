# wire Site Mode claims into gateway routing

## Status

Proposed

## Date

2026-08-09

## Related

#109, #110, #111, #112

## Goal

Mount the Site Mode APIs and route site agents through the relay while leaving direct agents on the current HTTP proxy.

## Owned paths

- `packages/fallow-coordinator/src/fallow_coordinator/app/factory.py`
- `packages/fallow-coordinator/src/fallow_coordinator/app/state.py`
- `packages/fallow-coordinator/src/fallow_coordinator/app/agent_routes.py`
- `packages/fallow-coordinator/src/fallow_coordinator/gateway/protocols.py`
- `packages/fallow-coordinator/src/fallow_coordinator/gateway/proxy.py`
- `packages/fallow-coordinator/src/fallow_coordinator/gateway/service.py`
- `packages/fallow-coordinator/src/fallow_coordinator/gateway/router.py`
- `packages/fallow-coordinator/tests/app/test_site_routes.py`
- `packages/fallow-coordinator/tests/gateway/test_site_transport.py`
- `packages/fallow-coordinator/src/fallow_coordinator/registry/sqlite_registry.py`
- `packages/fallow-coordinator/src/fallow_coordinator/rag/retrieval.py`
- `packages/fallow-coordinator/src/fallow_coordinator/rag/query.py`
- `packages/fallow-coordinator/src/fallow_coordinator/app/background.py`
- `docs/adr/082-lan-site-routing.md`

Four files outside the app/gateway composition are owned for additive
site-awareness hooks only, so a site agent is never dialed directly and its
relay work is fenced on every transition:

- `registry/sqlite_registry.py`: a read accessor for the agent's persisted
  `transport` and `presence_generation`, so the app layer never reaches into the
  registry connection or duplicates its schema.
- `rag/retrieval.py` and `rag/query.py`: an injected embed-fetch seam so RAG
  embedding routes through the same relay as chat, keeping RAG decoupled from the
  gateway (the seam is a plain callback, not a gateway import).
- `app/background.py`: the offline sweep invalidates a site agent's relay work.

No other registry, RAG or background behaviour changes.

No other path belongs to this PR. If implementation needs another existing file, stop and amend the specification before editing it.

## Contract

Agent routes add the authenticated claim GET, streaming response POST and failure POST from `relay-v1.md`. Path identity is checked with the existing bearer authentication. `USER_RETURNED`, reclaim and offline transitions invalidate relay work immediately.

Add an internal `ReplicaTransport` protocol with the current buffered and streaming acquire methods. `UpstreamProxy` remains the direct implementation. `SiteRelayTransport` uses `RelayBroker`. Selection reads the persisted agent transport before each attempt: `site_relay` always uses the relay and never calls `_url`; `direct` stays byte-for-byte on the current path.

The gateway preserves the current one-repick rule before the first response byte and never replays after it. Status, content type and raw SSE bytes pass through unchanged. Production Site Mode HTTP clients use `trust_env=False`; injected test clients are not replaced.

## Verification

Tests use a direct-dial bomb to prove a disconnected site agent cannot fall back to its registered host. Cover auth, wrong path identity, claim timeout, buffered and SSE parity, response upload disconnect, active-user invalidation, retry boundaries and legacy direct parity. Existing gateway, RAG and integration tests remain green.

## Compatibility

LAN Site Mode remains opt-in. Existing explicit URL and Tailscale behaviour must remain unchanged unless this specification says otherwise. The implementation must pass the repository's Python or Go quality gates for every lane it touches.

## Exclusions and honest gaps

No Go, CLI, installer, mDNS or public protocol changes. This PR is the sole Site Mode owner of the listed app and gateway composition files.

School VLAN, proxy, EDR, power and reimage behaviour are not proven by sandbox tests. Any applicable item remains a named manual gate.
