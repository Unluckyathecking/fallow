# fence stale presence and site routing

## Status

Proposed

## Date

2026-08-09

## Related

#109

## Goal

Make user presence ordering and the Site Mode routing decision explicit in persisted coordinator state.

## Owned paths

- `packages/fallow-coordinator/src/fallow_coordinator/registry/schema.sql`
- `packages/fallow-coordinator/src/fallow_coordinator/registry/sqlite_registry.py`
- `packages/fallow-coordinator/src/fallow_coordinator/registry/records.py`
- `packages/fallow-coordinator/src/fallow_coordinator/registry/mapping.py`
- `packages/fallow-coordinator/src/fallow_coordinator/registry/tunnel_mode.py`
- `packages/fallow-coordinator/src/fallow_coordinator/registry/README.md`
- `packages/fallow-coordinator/src/fallow_coordinator/scheduler/_eligibility.py`
- `packages/fallow-coordinator/tests/registry/test_site_presence.py`
- `packages/fallow-coordinator/tests/scheduler/test_site_presence.py`
- `docs/adr/081-lan-site-presence-fencing.md`

No other path belongs to this PR. If implementation needs another existing file, stop and amend the specification before editing it.

## Contract

Enrollment tokens gain `mode` with `legacy` as the migration default. Registered agents persist `transport=direct|site_relay`, also defaulting to `direct`. Agent rows store a presence fence and generation.

A Site Mode presence event carries `detail["sequence"]`. Applying a newer user-return, user-idle or reclaim transition advances `presence_generation` and records its sequence. `record_heartbeat` may refresh telemetry but cannot overwrite presence state when its `seq` is below the fence. Repeated or older transitions are idempotent.

Expose one internal predicate: fresh, IDLE, not `serving_paused`, matching READY replica and, for Site Mode, an active claim waiter supplied by the caller. Batch eligibility also rejects `serving_paused`. Legacy endpoint mapping keeps the stored direct host and port.

## Verification

Migration tests open old databases and retain direct routing. Race tests delay an IDLE heartbeat until after a newer user-return event. Reclaim, suspect/offline, READY state, token rollback, duplicate events and restart persistence are covered. Existing direct gateway and scheduler fixtures stay unchanged.

## Compatibility

LAN Site Mode remains opt-in. Existing explicit URL and Tailscale behaviour must remain unchanged unless this specification says otherwise. The implementation must pass the repository's Python or Go quality gates for every lane it touches.

## Exclusions and honest gaps

No FastAPI route, relay broker, gateway composition, public protocol schema or Go code. The event sequence stays in the existing detail map to avoid a cross-lane wire migration.

School VLAN, proxy, EDR, power and reimage behaviour are not proven by sandbox tests. Any applicable item remains a named manual gate.


## Producer and consumer boundary

Enrollment mode stays on the coordinator's enrollment-token row. The existing protocol registration request is unchanged: token creation accepts an optional keyword-only mode, and registration consumes that mode atomically to persist `direct` or `site_relay` transport. The router consumes transport through its injected callback; a later integration change owns that wiring.

Presence fencing is a registry-only boundary. The registry accepts an agent id, event kind and sequence, advances the generation only for a newer sequence, and does not parse HTTP or protocol events. The route producer extracts `detail["sequence"]` and calls this method in a later integration change.
