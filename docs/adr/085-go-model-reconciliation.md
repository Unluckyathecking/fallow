# reconcile assigned models into replicas

## Status

Proposed

## Date

2026-08-09

## Related

#109

## Goal

Finish the missing Go agent seam that turns `desired_models` into verified loopback replicas.

## Owned paths

- `go-agent/reconcile/**`
- `docs/adr/085-go-model-reconciliation.md`

No other path belongs to this PR. If implementation needs another existing file, stop and amend the specification before editing it.

## Contract

`Reconciler.Apply(ctx, desired []string) error` computes additions and removals against supervisor status. For an addition it fetches the existing authenticated manifest and ranged blob, verifies the current modelcache store, allocates a configured local port and calls `Supervisor.StartReplica`. For a removal it calls `StopReplica` and retains the verified cached model.

Repeated desired sets are idempotent. Partial downloads use the current atomic modelcache behaviour. Concurrent Apply calls serialize. Site Mode passes loopback bind; the reconciler itself accepts an injected supervisor and store and owns no network trust policy.

## Verification

Tests cover add, remove, unchanged sets, interrupted and resumed download, hash mismatch, port exhaustion, start failure cleanup, cancellation and two rapid assignments. Run race tests and a Windows build. Use fakes for HTTP and supervisor process launch.

## Compatibility

LAN Site Mode remains opt-in. Existing explicit URL and Tailscale behaviour must remain unchanged unless this specification says otherwise. The implementation must pass the repository's Python or Go quality gates for every lane it touches.

## Exclusions and honest gaps

No heartbeat/runtime wiring, inference claims, config, installer or coordinator code. Do not reimplement modelcache or supervisor.

School VLAN, proxy, EDR, power and reimage behaviour are not proven by sandbox tests. Any applicable item remains a named manual gate.
