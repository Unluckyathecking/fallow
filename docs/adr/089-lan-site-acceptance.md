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

## Amendment: supervised claim-runner reconnect

The harness also proved the coordinator-restart recovery clause
("agents resume held polling after reconnect") failed: the Site Mode claim runner
returned on the first transient relay/transport error (a coordinator that dropped
its socket), so after the coordinator returned the agent kept heartbeating with a
READY replica but never resumed claim polling — serving stayed dark.

Fix, kept minimal. The claim runner is now supervised: a transient Claim/relay
transport error restarts polling after a bounded, context-cancellable exponential
backoff (reset once a run has stayed healthy), so a coordinator down/up cycle
resumes held polling on its own. Context cancellation stays terminal (shutdown),
and a genuine authentication or configuration failure still fails closed through
the existing heartbeat/auth path, which shares the device token and cancels the
run context — observed here as terminal. There is exactly one supervised runner:
no duplicate runners and no hot loop.

Additional owned paths for this amendment:

- `go-agent/runtime/site.go` (`superviseClaimRunner`)
- `go-agent/runtime/runtime.go` (start the supervised runner)
- `go-agent/runtime/site_reconnect_test.go` (backoff/cancellation/no-hot-loop unit coverage)

Regression coverage. `tests/integration/site_mode` restarts the coordinator on the
same origin while the agent keeps running and asserts the same agent resumes held
claim polling and serves after reconnect; the Go unit test proves the supervisor
backs off, does not hot loop, and returns promptly on cancellation.

## Windows lane evidence

Run on a real Windows 10 runner (overlord@100.87.108.10, drive D:) against this
branch's `deploy/windows` and a `windows/amd64` `agentctl.exe` built from this head:

- The merged Pester suite `deploy/windows/tests/site-mode.Tests.ps1` — strict join
  validation, TOML escaping, token-free render, the ACL command shape, `install.ps1
  -DryRun` task rendering and legacy parity, and the doctor JSON contract — passes
  98/98.
- A real `install.ps1` -> `doctor.ps1` -> scheduled-task -> `uninstall.ps1 -Purge`
  cycle passes end to end: the rendered config is token-free and carries only
  `site_join_bundle`, the join and config land under an owner-only ACL, the bind is
  loopback, the staged Windows `llama-server.exe` resolves, the SPKI pins validate
  from the join file, the `FallowAgent` scheduled task registers and then
  unregisters cleanly, and `doctor` reports `ok: true`.

Honest gaps (loudly reported, not skipped). Over a headless SSH session `doctor`
reports `interactive_session` false ("no logged-in user; the at-logon task cannot
run until someone signs in") and `task_running` not-yet-run. The at-logon run and
Windows input-preemption need a real interactive desktop session, so they remain
named manual gates rather than sandbox-proven. School VLAN, proxy, EDR, power and
reimage persistence likewise remain manual gates; passing CI does not claim them.