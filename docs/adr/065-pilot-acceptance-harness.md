# ADR 065: Phase-A pilot acceptance harness

**Status:** Accepted

**Date:** 2026-07-17

## Context

`docs/school-pilot.md` section 8 gives an IT admin a nine-row table to run
before a wider pilot, plus the uninstall check in section 6. The rows describe
observable behaviour: the agent restarts at login, yields when the user returns,
reroutes when an agent dies, rejects a corrupt model, logs metadata only.

Some of those are things we can check here with fakes. Others need a real
machine, a running fleet, or a person at the keyboard. Until now the list lived
only as prose, so nothing stopped a check from drifting out of the code, and an
admin had no single command to see what CI already covers versus what they still
have to run by hand.

The installers were written in a sandbox with no macOS or Windows service host,
so the `launchd` and Task Scheduler registration is the least-tested part of the
whole system. Section 8 exists partly to verify that wiring on real hardware.

## Decision

Add a small acceptance harness under `tests/acceptance/`. One module,
`acceptance_matrix.py`, holds every Phase-A row as data: an id, the section it
traces to, whether CI asserts it or a human runs it, the exact command, and the
expected result. That module is the single source of truth. `acceptance_runner.py`
prints it as a checklist an admin can read; run it with
`python tests/acceptance/acceptance_runner.py`.

`test_acceptance_matrix.py` keeps the matrix honest: every automated row must
name a test file that exists on disk, and every manual row must carry a command
and an expected result. A dropped check fails the build.

Five rows run in CI with no real device, model, network, or GPU:

- **Clean install and login persistence.** The tests drive the installers'
  existing dry-run seam (`FALLOW_INSTALL_DRY_RUN=1` on macOS, `-DryRun` on
  Windows) with the prebuilt-binary flavour, so the render needs no uv or venv.
  They assert the rendered launch item and that a dry run writes nothing, then
  assert the template wiring directly (`RunAtLoad` and `KeepAlive` on the
  LaunchAgent, a logon trigger and restart-on-failure and no boot trigger on the
  scheduled task) so the persistence checks hold on any host, including one where
  the platform installer will not run.
- **Uninstall completeness.** The test runs the real macOS uninstaller against a
  throwaway `HOME` seeded with a planted launch item and `~/.fallow` tree, and
  asserts the launch item is removed, state is preserved without the purge flag,
  and deleted with it.
- **Model-corrupt rejection.** Already covered by the agent's own unit test at
  `packages/fallow-agent/tests/modelcache/test_modelcache_verify.py`, which
  drives the real model cache with a mocked transport and asserts a sha256 or
  size mismatch refuses to serve. The matrix points at it rather than duplicating
  the verifier here.
- **Log hygiene.** The test writes a `GatewayLogEntry` through the real JSONL
  writer for a request whose prompt holds a secret, then asserts the line carries
  only the record's metadata fields and leaks neither the prompt nor any content
  or identity field.

Five rows stay manual because they need state this harness cannot fake, so each
carries the exact command and expected result: user-return preemption, killed-
agent reroute, coordinator-restart recovery, network-removed backoff, and
active-user suspend.

## Consequences

The automated rows guard the wiring and the contracts on every push. A green run
is not a substitute for section 8 on real hardware: the dry-run render proves the
launch item is correct, not that `launchd` or Task Scheduler accepts it, and the
uninstall test proves file-level removal, not that orphaned replica ports and
processes are freed on a serving machine. Those remain the manual checks the
runner prints.

The harness reaches into `fallow_coordinator` and the agent's test suite for the
log and model-corrupt rows. That is the same cross-tree reach the integration
suite already takes and sits outside the import-linter contracts, which govern
the shipped packages, not the tests.

Scope is Phase A only. Phase B (load, multi-tenant isolation, the security
audit) is out of scope and tracked on the roadmap.
