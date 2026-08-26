<#
.SYNOPSIS
    Remove the Fallow agent from Windows.

.DESCRIPTION
    Stops and unregisters the at-logon task, stops any running agent and
    llama-server replica processes (which frees the ports they bound), and
    removes the LLAMA_ARG_THREADS cap install.ps1 sets on the CPU fallback.

    By default it PRESERVES %USERPROFILE%\.fallow (config, model cache, logs).
    Pass -Purge to delete that per-user state too. It never touches the git
    checkout or deploy\bin. -WhatIf shows what would happen and changes nothing.

    -User <name> is the mirror of install.ps1 -User: run elevated from an admin
    or SYSTEM context and it acts on that account's profile and environment
    instead of this one's. The Scheduled Task is machine-wide either way -
    \Fallow\FallowAgent is one registration per machine, whoever it runs as.

.PARAMETER Purge
    Also delete %USERPROFILE%\.fallow (or, with -User, that account's copy).

.PARAMETER User
    Remove the install belonging to another account, from an elevated admin or
    SYSTEM context. Requires elevation.
#>
[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'Medium')]
param(
    [switch]$Purge,
    [string]$User
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Log { param([string]$Message) Write-Host "[uninstall] $Message" }
function Throw-Err { param([string]$Message) throw "[uninstall] ERROR: $Message" }

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $ScriptDir 'lib\target-user.ps1')

$TaskName   = 'Fallow\FallowAgent'
# Get/Stop/Unregister-ScheduledTask resolve by leaf name + folder, not the
# combined path string, so the teardown must pass them split to find the task.
$TaskLeaf   = 'FallowAgent'
$TaskFolder = '\Fallow\'
$ThreadEnv  = 'LLAMA_ARG_THREADS'

# Whose install is being removed: this account's, or a nominated one from an
# admin context. Resolve before touching anything so a bad name fails first.
if ($PSBoundParameters.ContainsKey('User') -and [string]::IsNullOrEmpty($User)) {
    Throw-Err '-User requires a non-empty account name'
}
if ($User) {
    if (-not (Test-FallowElevated)) {
        Throw-Err "-User removes another account's install; run it elevated (an admin shell, or SYSTEM under Intune/ConfigMgr/PDQ/GPO)"
    }
    $Target     = Resolve-FallowTargetUser -Name $User
    $UserId     = $Target.Name
    $UserSid    = $Target.Sid
    $FallowHome = Join-Path $Target.ProfilePath '.fallow'
    Write-Log "admin context: removing the install belonging to $UserId"
} else {
    $UserId     = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $UserSid    = $null
    $FallowHome = Join-Path $env:USERPROFILE '.fallow'
}

function Stop-FallowProcesses {
    <#
    .SYNOPSIS
        Stop agent and replica processes so no port stays bound.
    .DESCRIPTION
        llama-server.exe and agentctl.exe are matched by image name; the Python
        flavour runs as pythonw.exe, so those are matched by a fallow_agent
        command line to avoid killing unrelated interpreters. Its own
        SupportsShouldProcess inherits the script's -WhatIf.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param()

    $targets = @()
    try {
        $procs = Get-CimInstance -ClassName Win32_Process -ErrorAction Stop
    } catch {
        Write-Log 'could not enumerate processes; skipping process cleanup'
        return
    }
    foreach ($p in $procs) {
        $name = $p.Name
        if ($name -eq 'llama-server.exe' -or $name -eq 'agentctl.exe') {
            $targets += $p
        } elseif ($name -eq 'pythonw.exe' -and $p.CommandLine -and $p.CommandLine -match 'fallow_agent') {
            $targets += $p
        }
    }
    foreach ($t in $targets) {
        if ($PSCmdlet.ShouldProcess("$($t.Name) (pid $($t.ProcessId))", 'stop process')) {
            Stop-Process -Id $t.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Log "stopped $($t.Name) (pid $($t.ProcessId))"
        }
    }
    if (-not $targets) { Write-Log 'no agent or replica processes running' }
}

if ($PSCmdlet.ShouldProcess($TaskName, 'stop and unregister scheduled task')) {
    Write-Log "stopping and unregistering $TaskName  (exercised in CI on windows-latest - verify on target)"
    Stop-ScheduledTask -TaskName $TaskLeaf -TaskPath $TaskFolder -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskLeaf -TaskPath $TaskFolder -Confirm:$false -ErrorAction SilentlyContinue
}

Stop-FallowProcesses

if ($UserSid) {
    # Another account's User scope is its registry hive, readable only while it
    # is signed in. Nothing else here depends on it, so say so and carry on.
    if ($null -ne (Get-FallowTargetEnv -Sid $UserSid -Name $ThreadEnv)) {
        if ($PSCmdlet.ShouldProcess("$UserId environment $ThreadEnv", 'clear')) {
            [void](Set-FallowTargetEnv -Sid $UserSid -Name $ThreadEnv -Value '')
            Write-Log "cleared $ThreadEnv from $UserId"
        }
    } elseif (-not (Test-FallowUserHiveLoaded -Sid $UserSid)) {
        Write-Log "note: $UserId is signed out, so any $ThreadEnv cap in their environment cannot be cleared from here"
    }
} elseif ($null -ne [Environment]::GetEnvironmentVariable($ThreadEnv, 'User')) {
    if ($PSCmdlet.ShouldProcess("user environment $ThreadEnv", 'clear')) {
        [Environment]::SetEnvironmentVariable($ThreadEnv, $null, 'User')
        Write-Log "cleared $ThreadEnv from the pilot account"
    }
}

if ($Purge) {
    if ($PSCmdlet.ShouldProcess($FallowHome, 'delete per-user state')) {
        Remove-Item -Recurse -Force $FallowHome -ErrorAction SilentlyContinue
        if (Test-Path -LiteralPath $FallowHome) {
            # An admin-context install grants Administrators and SYSTEM on the
            # Site directory, so this purge removes it. A desk installed the old
            # way - from the account's own session - granted the task user alone,
            # and only that install needs ownership taken first.
            Write-Log "WARNING: $FallowHome survived the purge. If it was installed from $UserId's own session rather than with install.ps1 -User, its Site state is granted to $UserId alone: take ownership (takeown /f `"$FallowHome`" /r /d y) or purge from that account's session."
        } else {
            Write-Log "purged $FallowHome"
        }
    }
} else {
    Write-Log "preserved $FallowHome (config, models, logs); re-run with -Purge to delete it"
}

Write-Log 'done'
