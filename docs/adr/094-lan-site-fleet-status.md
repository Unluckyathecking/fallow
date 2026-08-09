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

Plus the paths amended under the "Amendment" sections below.

No other path belongs to this PR. If implementation needs another existing file, stop and amend the specification before editing it.

## Contract

`flw site status` renders one row per Site Mode agent: agent id, enrollment mode, transport, last heartbeat age, presence state and generation, availability, ready replica count, and the last claim outcome or typed failure code. Human and `--json` output carry the same fields. Direct agents are out of scope.

The coordinator gains one authenticated read-only admin route under the existing site router. It reads through existing registry accessors and the relay broker's public state; it adds no new storage and mutates nothing. The CLI reaches it through the same admin key sources and direct no-proxy transport as `flw site join-bundles`. No token, pin or join material appears in any output.

## Verification

Unit tests cover the route, the renderer and the JSON shape, including an agent that has never claimed and an agent with a live typed failure. One case in the `tests/integration/site_mode` harness asserts the route reports the harness agent's real enrollment, presence and claim state.

## Compatibility

Read-only and additive. The route mounts only when Site Mode is enabled, matching the join-bundle admin router. Existing CLI commands, explicit URL and Tailscale behaviour are unchanged.

## Amendment: the integration case has no owned file

Verification requires one case in the `tests/integration/site_mode` harness, but
the Owned paths list names no file in that directory, so the specification asks
for a test it forbids writing. The Verification clause is the intended one — the
route's whole point is that it reports the real vertical, not a fake — so the
owned set is what needs correcting.

Additional owned path for this amendment:

- `tests/integration/site_mode/test_fleet_status.py`

That one new file is the entire integration footprint. The existing harness
modules are read and reused, not edited: no other file under
`tests/integration/site_mode` belongs to this PR.

## Amendment: two contracted fields have no public source

The Contract asks for a per-agent heartbeat age and last claim outcome. Neither
is readable from the owned set, so two of the nine columns could not be built.

Heartbeat age. `AgentSnapshot` carries no `last_seen` and no age. `snapshots()`
computes the age, uses it to drop offline agents and to set `suspect`, then
discards it; `list_offline` and `replica_endpoints` likewise consume it and
return ids and endpoints. The column exists on `registry_agents`, but no
accessor returns it.

Claim outcome. The broker's only public read is `has_pending`. A terminating
claim is removed from the live registry immediately and its typed code goes with
it — `_terminal_class` retains only duplicate/gone, keyed by claim id, not by
agent — so the typed failure the operator needs is not retained anywhere, even
privately.

Fix, kept minimal and read-only. One registry accessor reads the site rows with
their heartbeat age in a single query, and the broker records how each agent's
last claim ended as it terminates. Both are additive: no schema change, no new
storage, nothing routing consults, and neither changes when a claim is fenced,
retried or invalidated.

Additional owned paths for this amendment:

- `packages/fallow-coordinator/src/fallow_coordinator/registry/sqlite_registry.py` (`site_fleet`)
- `packages/fallow-coordinator/src/fallow_coordinator/site_relay/broker.py` (`last_claim`)

Both accessors return a `NamedTuple` declared beside the method that produces
it, so the route reads them by attribute and neither package's `__init__.py`
changes. `site_fleet` deliberately keeps agents past `offline_after_s`, which
`snapshots` drops: a desk that stopped heartbeating is exactly what this view
exists to show, and its age is the evidence. It is a parallel reader — how
`snapshots`, `list_offline` and eligibility compute and consume `last_seen` is
untouched — and it reads `enrollment_mode` from its own column rather than
inferring it from transport.

The broker's record is observation only: one write where a claim already
terminates, and one accessor. It is keyed by agent id and overwritten on each
terminal, so it holds one record per agent and cannot outgrow the enrolled
fleet however many claims run. The broker has no path that forgets an agent, so
there is nothing to clear it alongside. No lifecycle, fencing, timing or
`_terminal_class` semantics change.
