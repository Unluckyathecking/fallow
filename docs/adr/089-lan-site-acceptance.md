# prove the static LAN Site Mode path

## Status

Proposed

## Date

2026-08-09

## Related

#109, #113, #114, #118, #119

## Goal

Provide the black-box release gate for the static-address, outbound-only school pilot path.

## Owned paths

- `tests/integration/site_mode/**`
- `docs/adr/089-lan-site-acceptance.md`

Plus the narrow production files amended under "Amendment: restart fence consistency" below.

Beyond those, no other path belongs to this PR. If implementation needs another existing file, stop and amend this specification before editing it.

## Contract

The harness starts a TLS coordinator, temporary database, Go agent and fake loopback llama without external network access. It mints a join file through `flw`, enrolls exactly once, assigns a model, waits for READY and sends buffered and SSE OpenAI requests.

Assertions cover: HTTPS only; wrong pin leaks no token; proxy traps untouched; bearer/path identity; loopback-only llama; no coordinator-to-agent socket; model add/remove; static address; fresh IDLE routing; immediate user-return suspension and removal; delayed heartbeat fence; active-midstream retry boundary; reclaim; coordinator and agent restart; token-free identity resume; and legacy explicit URL/Tailscale parity.

The Windows lane runs pinned join, claim, input-preemption and loopback smoke tests on a real Windows runner. The school checklist records the separate on-site network, EDR, power and persistence checks.

## Verification

Run the new integration directory, existing Go parity/preemption tests, full Python gates and full Go gates. All tests use bounded timeouts and record exact failures. A skipped Windows or preemption case is a failed acceptance run, not a pass.

## Compatibility

LAN Site Mode remains opt-in. Existing explicit URL and Tailscale behaviour must remain unchanged unless this specification says otherwise. The implementation must pass the repository's Python or Go quality gates for every lane it touches.

## Exclusions and honest gaps

No feature code, workflow YAML, mDNS, shared changelog or ADR index. Passing CI does not claim the school VLAN or EDR has been tested.

School VLAN, proxy, EDR, power and reimage behaviour are not proven by sandbox tests. Any applicable item remains a named manual gate.


## Amendment: restart fence consistency

The acceptance harness proved the vertical and, in doing so, surfaced a real
routing defect this gate exists to catch: a same-identity restart could not be
served.

Mechanism. The heartbeat and offline relay fences raised the in-memory broker
generation to `route.presence_generation + 1` without persisting that generation
to the registry. A graceful shutdown sends one DRAINING heartbeat; that
ineligible heartbeat fenced the agent's in-flight relay work and advanced the
broker fence, but the durable `presence_generation` stayed behind. On restart the
agent claimed at the persisted generation, the broker rejected it as stale, and
serving was stranded — with no reclaim/release or other presence event to ever
close the gap.

Fix, kept minimal. When a heartbeat/offline fence raises the broker generation it
now persists the same generation first (persist before advance), and it only
fences when the agent still holds relay work or a claim waiter, so an ineligible
heartbeat with nothing to drop never advances the generation pointlessly. The
durable `presence_generation` therefore never lags the in-memory fence, and a
restarted agent claims at a generation the broker still honours. Invalidation
strength is unchanged: an agent that goes ineligible while genuinely serving has
its in-flight work dropped exactly as before.

Additional owned paths for this amendment:

- `packages/fallow-coordinator/src/fallow_coordinator/app/agent_routes.py` (heartbeat fence)
- `packages/fallow-coordinator/src/fallow_coordinator/app/background.py` (offline fence)
- `packages/fallow-coordinator/src/fallow_coordinator/site_relay/broker.py` (`has_pending`)
- `packages/fallow-coordinator/src/fallow_coordinator/registry/sqlite_registry.py` (`bump_presence_generation`)

Regression coverage. `tests/integration/site_mode` asserts that a graceful
shutdown followed by a same-identity restart routes a relayed request
successfully, with no unrelated presence cycle.