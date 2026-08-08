# prove the static LAN Site Mode path

## Status

Proposed

## Date

2026-08-09

## Related

#109, coordinator routing PR, CLI join bundle PR, Go Site Mode runtime PR, Windows install PR

## Goal

Provide the black-box release gate for the static-address, outbound-only school pilot path.

## Owned paths

- `tests/integration/site_mode/**`
- `docs/adr/089-lan-site-acceptance.md`

No other path belongs to this PR. If implementation needs another existing file, stop and amend the specification before editing it.

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
