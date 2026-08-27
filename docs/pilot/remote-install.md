# Installing Fallow desks remotely

For IT deploying the Site Mode agent to a pilot fleet from a management tool
rather than by walking to each desk. It covers what changes in an admin context
and what does not, and gives the exact command lines for a remote shell, Intune
and a GPO startup script.

The agent itself does not move. It runs as an at-logon Scheduled Task in the
pilot user's own interactive session, because idle detection reads the input
time of that session and a service in session 0 cannot see it (ADR 017, 063).
What moves to the admin context is the **registration**: staging the files into
a nominated account's profile and registering that account's task.

## Prerequisites

- The desk bundle for the pilot version,
  `fallow-site-agent_<version>_windows_amd64.zip`, unzipped somewhere that stays
  put on the target machine (a local path, not a mapped drive). The scripts
  resolve each other by position in that layout.
- A llama.cpp build staged under `bin\windows\` inside the bundle. Either run
  `.\windows\fetch-llama.ps1` on the machine, or ship the bundle with that
  directory already populated. The installer refuses before writing anything if
  `llama-server.exe` is not there.
- **One join file per desk.** A join file carries a single-use enrollment token
  and is a credential, not configuration. Do not put join files in an MDM
  package, a share every desk reads, or a script body: one device, one file.
  Delivery is covered in the operator runbook (§3, per-device USB or per-device
  MDM delivery); nothing on this page changes it.
- **The nominated user must have signed in to the machine at least once**, so a
  profile exists. The installer refuses an account with no profile and says so.
  Creating profiles is out of scope: it will not make one.
- An elevated context on the target: admin or SYSTEM.

## (a) At the desk, in the pilot user's session

The original path, unchanged:

```powershell
.\bootstrap.ps1 -JoinBundle D:\join\desk-01.fallow-join -GoBinary .\agentctl.exe
```

Everything lands in `%USERPROFILE%`, and the task starts immediately.

## (b) Remote elevated shell, PDQ, ConfigMgr

One line, run elevated as any admin or as SYSTEM:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\fallow\windows\install.ps1 -User pilot -JoinBundle C:\fallow\join\desk-01.fallow-join -GoBinary C:\fallow\agentctl.exe
```

Call `windows\install.ps1` directly here, not `bootstrap.ps1`: the bootstrap is
the desk-side wrapper that reports the machine and self-tests afterwards in the
user's session, and it has no `-User`.

`-User` takes `pilot`, `MACHINE\pilot` or `DOMAIN\pilot`. Add `-WhatIf` for a
full rehearsal that changes nothing: it resolves the account, walks every step
and reports it, and touches neither the profile nor Task Scheduler.

What it does differently from (a):

- Resolves the account's profile from the ProfileList registry, not from
  `%USERPROFILE%`: a relocated or roaming profile lands correctly.
- Stages `agentctl.exe`, the join copy and `agent.toml` under **that** profile's
  `.fallow`, and grants the join copy to that account alone.
- Registers `\Fallow\FallowAgent` with the target as both the logon trigger and
  the principal, still `InteractiveToken` and `LeastPrivilege`.
- **Does not enrol.** No token is sent from the admin context and no coordinator
  is contacted. The agent enrols itself on its first run in the user's session,
  from the staged join file, then deletes its copy of the token.
- **Does not start the task.** It cannot: the task needs the user's interactive
  session. It runs at their next logon, which is what the at-logon trigger is
  for. If they are signed in now, sign them out and back in.

Removal is symmetrical:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\fallow\windows\uninstall.ps1 -User pilot -Purge
```

`-Purge` also deletes that profile's `.fallow`. Without it, config, models and
logs stay.

## (c) Intune Win32 app

Package the unzipped bundle (with `bin\windows\` staged) into a `.intunewin`
with the Microsoft Win32 Content Prep Tool, and set:

| Field | Value |
| --- | --- |
| Install command | `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\windows\install.ps1 -User pilot -JoinBundle .\join\desk-01.fallow-join -GoBinary .\agentctl.exe` |
| Uninstall command | `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\windows\uninstall.ps1 -User pilot -Purge` |
| Install behaviour | System |
| Detection rule | Custom script, the one below. Leave "Run script as 32-bit process on 64-bit clients" unchecked. |

Do not use a file rule on `C:\Users\pilot\...`. The installer deliberately does
not guess that path: it resolves the profile from `ProfileList`, so a roaming,
relocated or renamed profile stages somewhere else entirely and a literal-path
rule reports the app as absent on a desk where the install worked — which means
Intune reinstalls it on every evaluation. The detection script does the same
resolution the installer does:

```powershell
$user = 'pilot'
try {
    $sid = (New-Object System.Security.Principal.NTAccount($user)).Translate(
        [System.Security.Principal.SecurityIdentifier]).Value
} catch { exit 1 }
$item = Get-ItemProperty -ErrorAction SilentlyContinue `
    -LiteralPath "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\$sid"
if (-not $item) { exit 1 }
$agent = Join-Path ([Environment]::ExpandEnvironmentVariables($item.ProfileImagePath)) `
    '.fallow\bin\agentctl.exe'
if (-not (Test-Path -LiteralPath $agent)) { exit 1 }

# There is one \Fallow\FallowAgent per machine, so its mere existence says
# nothing about which account it was registered for: a desk installed for
# someone else would otherwise detect as installed for this one. Compare the
# principal as a SID (the task keeps whatever form it was registered with) and
# the action against the binary staged in this account's profile.
$task = Get-ScheduledTask -TaskName FallowAgent -TaskPath '\Fallow\' -ErrorAction SilentlyContinue
if (-not $task) { exit 1 }
try {
    $principal = $task.Principal.UserId
    if ($principal -notmatch '^S-1-') {
        $principal = (New-Object System.Security.Principal.NTAccount($principal)).Translate(
            [System.Security.Principal.SecurityIdentifier]).Value
    }
} catch { exit 1 }
if ($principal -ne $sid) { exit 1 }
if ($task.Actions[0].Execute -ne $agent) { exit 1 }
Write-Output 'installed'
exit 0
```

Intune reads exit code 0 with non-empty output as "detected". The checks
together mean the files are staged in this account's profile and the task is
registered **for this account, running that binary** — which is exactly what the
install guarantees and all it guarantees: enrolment happens later, in the user's
session. Detection does not prove a desk is serving. `doctor.ps1` does that, and
it must run in the pilot user's session.

The principal and action checks are not belt-and-braces. A desk that was
installed for a different account, or one whose profile moved after the task was
registered, has a `\Fallow\FallowAgent` that will never start this account's
agent; reporting it as installed leaves the desk permanently unenrolled and
Intune content.

The join file is per-device, so a single Win32 app for the whole fleet cannot
carry one. Either scope one app per device, or deliver join files separately and
have the install command point at the delivered path.

The installer's exit code is what Intune reads: non-zero with a `[install]
ERROR:` line on any refusal (no profile, no elevation, bad join file, no staged
llama build).

## (d) GPO startup script

A startup script runs as SYSTEM before anyone logs on, which works here: the
registration needs the profile to **exist**, not the user to be signed in right
now. The agent starts at their next logon regardless.

Two caveats:

- **The profile must already exist.** A fresh machine nobody has signed in to
  has no profile for the pilot account, and the install refuses. Startup scripts
  are therefore for machines already in use, not for imaging.
- **A signed-out account's environment can be read, but not written.** Windows
  mounts a user's registry hive only while they are signed in. To check their
  `FALLOW_*` overrides anyway — `FALLOW_STATE_PATH` decides whether this desk is
  already enrolled — the installer mounts their `NTUSER.DAT`, reads it, and
  unmounts it again. If the hive will not mount, because the profile is in a
  half-state or something else holds the file open, it prints a warning naming
  what it could not check and continues on the machine-scope values and
  `agent.toml`. It still cannot **set** the `LLAMA_ARG_THREADS` cap the CPU
  llama.cpp fallback uses; it says so and continues. Neither affects a CUDA desk
  with no overrides set. A value stored with a `%VAR%` in it is expanded as that
  account, not as whoever is running the installer, so `%USERPROFILE%` means
  their profile and never the installer's. `-WhatIf` and `-DryRun` both mount
  nothing, so a rehearsal against a signed-out account reports no per-user
  override either way.
- **Uninstalling with `-User` will not take down another account's task.** There
  is one `\Fallow\FallowAgent` per machine. `uninstall.ps1 -User <name>` removes
  it only when its principal and its action belong to the account named; where
  they do not — a retirement command for somebody who left, run on a desk since
  reinstalled for someone else — it leaves the task registered, says so, and
  still removes the named account's own files.

A logon script is not an alternative: it runs as the user, without elevation,
and this is the path for elevated contexts.

## What is not proven

Written and reviewed in a sandbox with no Windows host. CI now runs a real `-User`
install on `windows-latest` (a local account with its own profile, the task
registered for it, the ACLs and staged files asserted, then uninstalled), so those
steps are marked `(exercised in CI on windows-latest - verify on target)`. A runner
is not a managed desk: the task starting at that account's next logon, a domain
account with a roaming profile, and the Intune and GPO paths above are documented
from the scripts' behaviour, not from a run in a managed tenant. Verify one machine of
each kind before a fleet roll-out, and read
[`docs/pilot/it-checklist.md`](it-checklist.md) for the endpoint-protection and
SmartScreen gates that apply either way.

One more shape to know: `\Fallow\FallowAgent` is a single machine-wide task
registration whoever it runs as. A desk serves for one nominated pilot account;
installing for a second account replaces the first one's task.
