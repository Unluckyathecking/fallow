# report live Site Mode fleet status

## Status

Proposed

## Date

2026-08-09

## Related

#110, #112, #114, #120

## Goal

Give the pilot operator one read-only view of every Site Mode agent so a four-machine pilot day does not require walking between desks: enrollment state, heartbeat age, presence, availability, replicas and the last claim outcome, from a single command.

## Owned paths

- `packages/fallow-cli/src/fallow_cli/site/status.py`
- `packages/fallow-cli/src/fallow_cli/main.py`
- `packages/fallow-cli/tests/site/test_status.py`
- `packages/fallow-coordinator/src/fallow_coordinator/site/router.py`
- `packages/fallow-coordinator/tests/site/test_status_route.py`
- `docs/adr/094-lan-site-fleet-status.md`

No other path belongs to this PR. If implementation needs another existing file, stop and amend the specification before editing it.

## Contract

`flw site status` renders one row per Site Mode agent: agent id, enrollment mode, transport, last heartbeat age, presence state and generation, availability, ready replica count, and the last claim outcome or typed failure code. Human and `--json` output carry the same fields. Direct agents are out of scope.

The coordinator gains one authenticated read-only admin route under the existing site router. It reads through existing registry accessors and the relay broker's public state; it adds no new storage and mutates nothing. The CLI reaches it through the same admin key sources and direct no-proxy transport as `flw site join-bundles`. No token, pin or join material appears in any output.

## Verification

Unit tests cover the route, the renderer and the JSON shape, including an agent that has never claimed and an agent with a live typed failure. One case in the `tests/integration/site_mode` harness asserts the route reports the harness agent's real enrollment, presence and claim state.

## Compatibility

Read-only and additive. The route mounts only when Site Mode is enabled, matching the join-bundle admin router. Existing CLI commands, explicit URL and Tailscale behaviour are unchanged.
