# install a desk without walking to it

## Status

Proposed

## Date

2026-08-26

## Goal

Let school IT deploy the Site Mode agent the way they deploy everything else:
from an elevated management context — Intune, ConfigMgr, PDQ, a GPO startup
script — running as some other account, usually SYSTEM.

Today `deploy\windows\install.ps1` has to run in the pilot user's own signed-in
session, because everything it does is anchored to `%USERPROFILE%` and to the
identity of whoever typed the command. That means someone walks to each desk,
signs in as the pilot account or borrows the keyboard from the person using it,
and repeats. Four desks is a morning. A year-group lab is not a pilot any more,
it is a project.

## Owned paths

- `deploy/windows/install.ps1` (the `-User` parameter set)
- `deploy/windows/uninstall.ps1` (the symmetrical `-User`)
- `deploy/windows/lib/target-user.ps1` (new)
- `deploy/windows/new-site-config.ps1` (`Protect-FallowSitePath -AlsoAllow`,
  `Expand-FallowHome`/`Resolve-FallowStatePath` profile and env parameters)
- `deploy/windows/tests/site-mode.Tests.ps1`
- `deploy/site-bundle.sh`, `tests/deploy/test_site_bundle.py` (one new file in
  the bundle)
- `docs/pilot/remote-install.md` (new), `docs/lan-site/operator-runbook.md` §4,
  `deploy/SITE-BUNDLE.md` (one cross-link each)
- `docs/adr/101-windows-admin-context-install.md`, `CHANGELOG.md`

No agent, coordinator or Python code is touched.

## Decision

**The registration moves to the admin context; the agent does not.**

```powershell
.\windows\install.ps1 -User pilot -JoinBundle .\join\desk-01.fallow-join -GoBinary .\agentctl.exe
```

The agent still runs as an at-logon Scheduled Task with `InteractiveToken` and
`LeastPrivilege` in the pilot user's own session. That is not a detail to
revisit: idle detection calls `GetLastInputInfo`, which reports the input time of
the active interactive session, and a service in session 0 has no input desk
(ADR 017, 063). Nothing here changes the principal, the logon type or the trigger
— only who performs the registration.

**One parameter, not a second script.** Only three things differ between the two
contexts: the profile the files land in, the identity on the ACLs and the task
principal, and which registry hive holds the user's environment. Everything else
— join validation, the enrolled-identity preflight, the token-free config render,
the ACL rebuild, the task XML, `-WhatIf`, `-DryRun` — is identical. A separate
`install-admin.ps1` would either duplicate all of that or shrink to a wrapper
that sets three variables, and the desk bundle would have to ship and hash a
second installer that must not drift from the first. So `-User` resolves those
three values at the top and the rest of the script reads them.

**The profile comes from the ProfileList registry.** `%USERPROFILE%` belongs to
the installing account, and `C:\Users\<name>` is a guess that a roaming,
relocated or renamed profile breaks. `HKLM\...\CurrentVersion\ProfileList\<SID>`
is the authority, and its absence is the signal that the account has never signed
in here.

**What is refused, and why.**

- *No profile* — refused, naming the account and the remedy (sign in once).
  Creating a profile from an installer means either `CreateProfile` P/Invoke or
  a fake logon, both of which produce a profile subtly unlike the real one. Out
  of scope, said plainly, rather than half-done.
- *Not elevated* — refused. Writing into another profile and registering another
  account's task both need it, and failing at the first `Set-Acl` instead is
  worse.
- *`-User` without `-GoBinary`* — refused. The Python flavour bootstraps a uv
  venv in the installing account's context, which is the wrong account. Site
  Mode already requires the Go agent, so this costs nothing real.
- *A config the admin context cannot read* — refused, before anything is
  written. A desk installed the old way leaves an owner-only `agent.toml`; every
  path below that read decides what to keep, and an installer that cannot read a
  live config must not overwrite it.

**No enrollment, and no task start, from the admin context.** This is a property
of the existing design, not a new rule: `install.ps1` has never enrolled.
`Runtime.resolveSite` reads the join file at the daemon's first run
(`go-agent/runtime/site.go`), registers once against the pinned coordinator,
persists the identity and site profile, then deletes its copy of the token. The
installer only validates the join file locally and stages it. So an admin-context
install sends nothing to the network and holds no token in memory, and the desk
enrols on the pilot user's next logon exactly as it does today. The task is not
started either: an `InteractiveToken` task has no session to start in from here,
and the at-logon trigger is precisely what covers the wait.

**Trust story: token exposure is unchanged.** `join.json` keeps its single grant
to the task user, in both contexts, and it is still protected before the token is
copied into place. What admin mode also grants `BUILTIN\Administrators` and `NT
AUTHORITY\SYSTEM` is the two containers around it: `.fallow\site\`, which the
installer has to write the copy into, and `agent.toml`, which it has to read back
on a re-run. Granting the directory does not grant the file — `join.json` carries
its own protected DACL — and `agent.toml` is token-free by construction, which is
the whole point of the join-bundle design (ADR 088). Both principals already hold
everything else in the profile by inheritance, including `agent-state.json` with
the persisted device token. `doctor.ps1`'s restrictiveness check is about broad
principals (Everyone, Users, Authenticated Users, INTERACTIVE) and is unaffected.
Nothing is written to a machine-wide location.

**Signed out means their environment is out of reach.** Windows mounts a user's
hive under `HKEY_USERS` only while they are signed in. When the target is signed
out, the installer cannot read their `FALLOW_*` overrides (it checks the machine
scope and says the per-user scope was unreadable) and cannot set the
`LLAMA_ARG_THREADS` cap the CPU llama.cpp fallback wants (it says so and
continues). Loading `NTUSER.DAT` by hand to reach them was rejected: it is
invasive, it fails outright while the user is signed in, and the cost of not
doing it is one warning on a CPU desk. It never touches correctness or the token.

## Verification

The Pester suite gains the admin-context lane: canonical name/SID/profile
resolution for a real account, the never-signed-in refusal (a principal that
resolves to a SID with no ProfileList entry has the same shape), a
`HKEY_USERS` environment round-trip and the unloaded-hive answers, the
`-AlsoAllow` ACL grant beside the one-grant default, `Resolve-FallowStatePath`
ignoring the installing process's environment when the target's value is
supplied, the four refusal paths, the rendered task naming the target account and
its profile, and `-WhatIf` staging nothing. The elevation-dependent cases skip
themselves where the suite is not running elevated.

`tests/deploy/test_site_bundle.py` covers the one new file: it must be in the
bundle, and the closure test proves every `$ScriptDir`-relative reference still
lands from the unzipped layout.

## Compatibility

Additive. Without `-User`, `install.ps1` and `uninstall.ps1` do exactly what they
did: same profile, same identity, same ACLs, same task, same start-now. Every new
refusal is inside the `-User` branch. The task definition, the join-bundle format
and the enrollment path are untouched.

## Exclusions and honest gaps

**Never executed on a real Windows host.** Authored in a Linux sandbox with no
PowerShell. Every new call — `NTAccount.Translate`, the ProfileList and
`HKEY_USERS` reads and writes, `Set-Acl` for another identity,
`Register-ScheduledTask` with another account's principal — carries the
`(untested - verify on target)` mark the rest of `deploy/windows` uses. One
managed desk of each kind is a pilot-day gate.

**Intune and GPO are documented, not proven.** `docs/pilot/remote-install.md`
gives the install and uninstall command lines and a detection rule (staged binary
plus the task's `TaskCache\Tree` key) derived from what the installer actually
guarantees. Nothing in it has been run in a tenant, and it invents no screenshots.

**The Pester suite is not run by CI.** No workflow executes
`deploy/windows/tests/site-mode.Tests.ps1` — it is written for Pester 3.4 on a
Windows host and runs there. The new cases inherit that gap, and half of them
additionally skip themselves without elevation. Wiring a Windows PowerShell lane
into CI is worth doing and is not this change.

**One task per machine.** `\Fallow\FallowAgent` is a single machine-wide
registration whoever it runs as, so a desk serves for one nominated account and
installing for a second replaces the first one's task. That matches the pilot —
one pilot account per desk — and per-user task names are a bigger change than the
problem justifies today. It is stated in the guide rather than hidden.

**Purging another account's state can need ownership.** `uninstall.ps1 -User
<name> -Purge` deletes what it can; the Site state directory is granted to the
task user alone, so an admin context may have to take ownership first. The script
detects the survivor and prints the `takeown` line instead of claiming success.

**Join-file delivery is unchanged and stays out of MDM content.** A join file
carries a live single-use token; it is per-device and belongs to the runbook's
USB or per-device delivery path, not in a package every desk unpacks. The guide
says so and does not offer a fleet-wide alternative.
