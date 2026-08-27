# install a desk without walking to it

## Status

Proposed

## Date

2026-08-26

## Goal

Let school IT deploy the Site Mode agent the way they deploy everything else:
from an elevated management context (Intune, ConfigMgr, PDQ, a GPO startup
script) running as some other account, usually SYSTEM.

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
(ADR 017, 063). Nothing here changes the principal, the logon type or the trigger:
only who performs the registration.

**One parameter, not a second script.** Only three things differ between the two
contexts: the profile the files land in, the identity on the ACLs and the task
principal, and which registry hive holds the user's environment. Everything else
(join validation, the enrolled-identity preflight, the token-free config render,
the ACL rebuild, the task XML, `-WhatIf`, `-DryRun`) is identical. A separate
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

- *No profile*: refused, naming the account and the remedy (sign in once).
  Creating a profile from an installer means either `CreateProfile` P/Invoke or
  a fake logon, both of which produce a profile subtly unlike the real one. Out
  of scope, said plainly, rather than half-done.
- *Not elevated*: refused. Writing into another profile and registering another
  account's task both need it, and failing at the first `Set-Acl` instead is
  worse.
- *`-User` without `-GoBinary`*: refused. The Python flavour bootstraps a uv
  venv in the installing account's context, which is the wrong account. Site
  Mode already requires the Go agent, so this costs nothing real.
- *A config the admin context cannot read*: refused, before anything is
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
on a re-run. Granting the directory does not grant the file (`join.json` carries
its own protected DACL), and `agent.toml` is token-free by construction, which is
the whole point of the join-bundle design (ADR 088). Both principals already hold
everything else in the profile by inheritance, including `agent-state.json` with
the persisted device token. `doctor.ps1`'s restrictiveness check is about broad
principals (Everyone, Users, Authenticated Users, INTERACTIVE) and is unaffected.
Nothing is written to a machine-wide location.

**Signed out, their environment is read out of NTUSER.DAT; it is still not
written.** Windows mounts a user's hive under `HKEY_USERS` only while they are
signed in, which in an admin context is precisely when they are not. The first
version of this ADR rejected loading `NTUSER.DAT` by hand as invasive and not
worth one warning on a CPU desk. That was right about the thread cap and wrong
about the scope: `FALLOW_STATE_PATH` lives in the same per-user store, and it is
what relocates an enrolled agent's identity. Unread, it makes an enrolled desk
look fresh, and the install then stages a join bundle whose single-use token the
resuming agent never consumes or deletes — a live enrollment credential left on
disk, which is a different class of cost from an uncapped thread count.

So `Get-FallowTargetEnvOffline` mounts it: `reg load` into a private
`HKEY_USERS` key, read `FALLOW_STATE_PATH`, `FALLOW_BIND_HOST` and
`FALLOW_SITE_JOIN_BUNDLE` — the three the preflight consults — then `reg unload`
in a `finally`, with the registry provider's cached handles dropped first and
the unload retried, because hives release lazily. A mount left behind would stop
that profile loading at the next logon, so it is the one step here that is not
best-effort: if it never releases, the installer says so and names the manual
`reg unload`. Not under `-WhatIf`, which mounts nothing and says it read no
per-user override: a rehearsal that died between the load and the unload would
leave the hive mounted, and `-WhatIf` promises a walk with no side effects.

**A hive that will not mount warns; it does not refuse.** A profile in a
half-state holds its own NTUSER.DAT open, and an MDM run lands on desks that
signed out seconds ago. Refusing there would fail installs on the path this
feature exists for — the genuinely fresh desk — for a reason the operator cannot
see from the outside, and it would fail the common case (nothing set) to protect
against the rare one. A fresh desk's hive mounts and reads back empty, which is
the case the tests pin. What is left is narrow, and named rather than silent:
already enrolled, relocated by a per-user `FALLOW_STATE_PATH` alone (the machine
scope and `agent.toml` are both still read), and a hive that will not mount. The
warning says which join file to check after the next logon and delete if it
survives.

The `LLAMA_ARG_THREADS` cap is still not written to a signed-out hive. Writing
into another account's hive is a different trade from reading it, and what it
costs is politeness on a CPU desk, not a credential.

One residual race, stated plainly: if the target signs in during the moment the
hive is mounted, Windows cannot load their profile and hands them a temporary
one. The window is the length of a registry read.

## Verification

The Pester suite gains the admin-context lane: canonical name/SID/profile
resolution for a real account, the never-signed-in refusal (a principal that
resolves to a SID with no ProfileList entry has the same shape), a
`HKEY_USERS` environment round-trip and the unloaded-hive answers, the offline
NTUSER.DAT read (a hive built with `reg save` stands in for a signed-out
profile: a relocated `FALLOW_STATE_PATH` reaching the disposition check as
`site` where a blind resolve still answers `fresh`, the empty map a fresh desk
gives back, the mount being released, and the two null answers that put the
caller on the warning path), the
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
PowerShell. Every new call (`NTAccount.Translate`, the ProfileList and
`HKEY_USERS` reads and writes, `Set-Acl` for another identity,
`Register-ScheduledTask` with another account's principal) carries the
`(untested - verify on target)` mark the rest of `deploy/windows` uses. One
managed desk of each kind is a pilot-day gate.
([ADR 102](102-install-acceptance-ci.md) later ran all of them on
`windows-latest`, including a real `-User` install for a local account with its
own profile, and re-marked them accordingly. A hosted runner is still not a
managed desk with a domain account.)

**Intune and GPO are documented, not proven.** `docs/pilot/remote-install.md`
gives the install and uninstall command lines and a detection rule derived from
what the installer actually guarantees: a script that resolves the account's SID
and `ProfileImagePath` the same way the installer does, then checks the staged
binary and the registered task. A literal `C:\Users\<name>` file rule is called
out as wrong — it reports the app absent on the relocated profiles the installer
handles correctly, and Intune reinstalls for ever. Nothing here has been run in
a tenant, and it invents no screenshots.

**The Pester suite is not run by CI.** No workflow executes
`deploy/windows/tests/site-mode.Tests.ps1`: it is written for Pester 3.4 on a
Windows host and runs there. The new cases inherit that gap, and half of them
additionally skip themselves without elevation. Wiring a Windows PowerShell lane
into CI is worth doing and is not this change.
(Closed by [ADR 102](102-install-acceptance-ci.md): the `windows-pester` job in
`install-acceptance.yml` runs the suite on `windows-latest` under Windows
PowerShell 5.1 with Pester pinned to 3.4.0. The runner is elevated, so the
elevation-gated cases run rather than skip.)

**One task per machine.** `\Fallow\FallowAgent` is a single machine-wide
registration whoever it runs as, so a desk serves for one nominated account and
installing for a second replaces the first one's task. That matches the pilot
(one pilot account per desk), and per-user task names are a bigger change than the
problem justifies today. It is stated in the guide rather than hidden.

**Purging an old-style install can need ownership.** `uninstall.ps1 -User <name>
-Purge` removes an admin-context install outright: `-AlsoAllow` granted
`Administrators` and `SYSTEM` on `.fallow\site`, and delete-child there is what
removes the owner-only `join.json` inside it. Ownership is only needed for a
profile installed the old way, from the account's own session, where the Site
state is granted to that account alone. The script detects the survivor and
prints the `takeown` line, scoped to that case, instead of claiming success.

**Join-file delivery is unchanged and stays out of MDM content.** A join file
carries a live single-use token; it is per-device and belongs to the runbook's
USB or per-device delivery path, not in a package every desk unpacks. The guide
says so and does not offer a fleet-wide alternative.
