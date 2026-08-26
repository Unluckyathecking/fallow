# run the desk installers on real hosts

## Status

Proposed

## Date

2026-08-26

## Goal

Stop marking the registration layer of the installers "untested" when CI already
has the hosts that could test it.

Two specific gaps. The Pester suite `deploy/windows/tests/site-mode.Tests.ps1` —
990 lines of join-file validation, config rendering, ACL and admin-context cases —
is invoked by no workflow; it was written for a Windows host and CI has one on
every push. And the acceptance harness only dry-run renders the launch item:
[ADR 065](065-pilot-acceptance-harness.md) says so plainly, "the dry-run render
proves the launch item is correct, not that `launchd` or Task Scheduler accepts
it". A `windows-latest` runner is elevated and can register a real Scheduled
Task; a `macos-latest` runner can bootstrap a real LaunchAgent. Neither is a desk
with a person at it, so the point is to prove the layer a runner can honestly
prove, and to keep saying so about the rest.

## Owned paths

- `.github/workflows/ci.yml` (the `windows-pester` job)
- `.github/workflows/install-acceptance.yml` (new)
- `deploy/windows/install.ps1`, `deploy/windows/uninstall.ps1`,
  `deploy/windows/lib/target-user.ps1`, `deploy/windows/lib/backend.ps1`,
  `deploy/bootstrap.ps1`, `deploy/macos/install.sh`, `deploy/macos/uninstall.sh`,
  `deploy/README.md` (marker wording only)
- `docs/adr/065-pilot-acceptance-harness.md`,
  `docs/adr/101-windows-admin-context-install.md`,
  `docs/lan-site/operator-runbook.md`, `docs/school-pilot.md` (claims that were
  no longer true)
- `docs/adr/102-install-acceptance-ci.md`, `CHANGELOG.md`

No installer logic changes. Every script edit is a comment or a log string.

## Decision

Three lanes, each with one layer it proves and no claim beyond it.

**`windows-pester` in `ci.yml`.** Runs the Pester suite on `windows-latest` under
Windows PowerShell 5.1, with `Import-Module Pester -RequiredVersion 3.4.0`. The
pin is load-bearing: the suite is written for the Pester 3.4 that ships inside
5.1, the runner image also carries Pester 5, and an unpinned import takes the
highest version, whose `Should` syntax would reject the whole file. The job fails
on any failed test and also on zero passes, so a suite that silently fails to load
cannot report green. Because the runner is elevated, the elevation-gated
admin-context cases run instead of skipping themselves.

*Proves:* the pure validation logic and the Windows API calls it makes — ACL
rebuilds through `Set-Acl`/`icacls`, `NTAccount` translation, `HKEY_USERS`
environment reads and writes, the `-DryRun` task render — behave on Windows the
way they were written to.

**`install-acceptance.yml`, `windows-register`.** A `desk-bundle` job on
`ubuntu-latest` cross-builds `agentctl.exe` and cuts the release zip with
`deploy/site-bundle.sh`; the Windows job downloads that artifact and installs from
it. The bundle is built on Linux because `site-bundle.sh` needs `zip`, which Git
for Windows does not ship — the Windows image's archiver is 7-Zip and
`Expand-Archive`. The Windows job deliberately has no checkout: a desk has the zip
and its join file and nothing else, so installing from the unzipped bundle alone
is part of what the lane proves.

It stages a placeholder `llama-server.exe` and mints a join file whose coordinator
origin is a closed loopback port. Both are honest stand-ins for *this* lane:
enrollment is lazy, so the installer never dials the coordinator, and both
`agentctl doctor` and `install.ps1` only stat the llama binary. Neither stands in
for serving.

Then, for this account, `bootstrap.ps1 -GoBinary … -JoinBundle …`, and for a
freshly created local account with a real profile, `install.ps1 -User …`. Each
asserts, exactly: the task exists; its principal resolves to the intended SID
(compared as SIDs, since Task Scheduler may store either form); it holds one
trigger and one action, and the action is the staged `agentctl.exe` with the Go
arg vector; the binary, join copy and config are staged in the right profile; the
config carries the loopback bind, the managed join path and the staged llama path,
and neither the token nor the legacy token key; the join copy, the config and the
Site directory each have a protected DACL granting exactly the expected principals
— one for the join copy, three (task user, Administrators, SYSTEM) for the two
containers in the admin-context install; and the CPU fallback set a positive
`LLAMA_ARG_THREADS`. `doctor.ps1` then runs and its JSON is parsed: the four lanes
a desk must satisfy with no coordinator — `task_registered`, `config_acl`,
`loopback_bind`, `llama_binary` — are asserted true one by one, and every other
key is asserted present. The aggregate `ok`/exit code is printed, not gated on:
the coordinator is unreachable by construction. Finally `uninstall.ps1 -Purge`
(and `-User … -Purge`) must leave no task, no `.fallow` tree and no thread cap.

*Proves:* Task Scheduler accepts the rendered XML and stores it as rendered, in
both registration contexts; the ACL and staging logic produces what it claims on
a real NTFS volume; `doctor.ps1` reports a real install correctly offline; the
uninstall removes what it says it removes.

**`install-acceptance.yml`, `macos-register`.** Builds `agentctl` on the runner,
runs `render_test.sh` (which had also never run in CI — it skips off macOS), mints
`manifest.sha256` from what it just built so the installer's trust gate is
satisfied by a real hash rather than bypassed, then runs `install.sh --go-binary`.
`install.sh` bootstraps into `gui/$UID` because idle detection needs the Aqua
session, so the lane preflights `launchctl print gui/$UID` and fails with that
named if the runner has no such domain — a clear diagnosis beats an opaque
bootstrap error, and there is no degraded fallback pretending the deep layer ran.
It then asserts the plist exists and lints, that it runs the staged binary with
the Go arg vector, and — the actual point — that `launchctl print
gui/$UID/com.fallow.agent` succeeds and holds that program. It re-runs the
installer to prove the bootout/bootstrap idempotence, then `uninstall.sh --purge`
must remove the plist, the state, and the job from launchd.

*Proves:* `launchctl bootstrap` accepts the rendered plist into a real GUI domain,
the SHA256 trust gate passes a genuine binary, and `bootout` releases the job.

Both lanes run on `pull_request` and on pushes to `main`, filtered to `deploy/**`
and the workflow file, matching `go.yml`. No `|| true`, no `continue-on-error`,
no skipped assertion.

## Verification

The workflows cannot execute here, so what was checked is what could be: every
workflow file parses as YAML; every repository path the new YAML names exists in
the tree (a throwaway extractor stats each one); every flag the lanes pass to
`bootstrap.ps1`, `install.ps1`, `uninstall.ps1`, `doctor.ps1`, `install.sh`,
`uninstall.sh` and `site-bundle.sh` exists in those scripts; every file the lanes
reach for inside the desk bundle is in `site-bundle.sh`'s file list; `bash -n` on
the shell touched; and the repository gates (`ruff`, `mypy`, `lint-imports`,
`pytest tests/deploy`) stay green. The expected doctor outcomes were derived by
reading `runDoctor` in `go-agent/cmd/agentctl/main.go` and `doctor.ps1`, not
guessed: an unenrolled identity is `ok`, the llama check is a `stat`, and the
clock check reports "skew unknown" for an unreachable coordinator.

## Compatibility

Additive. No installer behaviour changes; the only script edits are comment and
log-string wording. The new workflow adds three jobs on `deploy/**` changes and
one always-on job (`windows-pester`) that needs no toolchain.

## Exclusions and honest gaps

**These lanes have never run.** They were authored in the same sandbox as the
installers they test, so the first execution is the first push of this branch.
Expect a fix-up round. The likeliest spots: `Start-ScheduledTask` on a runner with
no interactive logon session; `launchctl bootstrap gui/$UID` if the macOS runner's
session turns out not to be a GUI domain, which the preflight names rather than
failing obscurely; and creating the test account's profile with
`Start-Process -Credential -LoadUserProfile`, which is how the lane gets an
account `-User` will accept.

**Registration is not a logon start.** The lanes prove the scheduler and launchd
accept and hold the job. They do not prove the Windows task starts when a user
signs in, that the LaunchAgent survives a real login cycle, or that idle detection
reads a live input timer — a runner has no logon and nobody at the keyboard.
Section 8 of `docs/school-pilot.md` still owns those rows.

**No serving, no coordinator, no LAN.** The llama binary is a placeholder the
installer only stats, and the join file's coordinator origin is a closed loopback
port. Nothing here starts a replica, enrols against a coordinator, or crosses a
school VLAN. The `-User` install's CPU thread cap lands in a signed-out account's
hive, so it takes the documented warning branch and keeps its `(untested)` mark.

**EDR, SmartScreen and code signing are untouched.** A GitHub runner allows what a
managed school desk may not. The binaries stay unsigned.

**The Windows binary is cross-built from Linux.** It is the same
`CGO_ENABLED=0 GOOS=windows` build the desk bundle and `ci.yml` already produce,
and the lane runs it, but a GoReleaser release binary is not byte-identical to it.

**The runner's account is a local admin, not a domain user.** `-User` is proven
against a local account with a freshly created profile. A domain account, a
roaming or relocated profile, and Intune or GPO delivery remain
`docs/pilot/remote-install.md`'s pilot-day gates.
