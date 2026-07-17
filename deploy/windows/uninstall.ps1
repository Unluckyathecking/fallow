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

.PARAMETER Purge
    Also delete %USERPROFILE%\.fallow.
#>
[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'Medium')]
param(
    [switch]$Purge
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Log { param([string]$Message) Write-Host "[uninstall] $Message" }

$TaskName   = 'Fallow\FallowAgent'
$FallowHome = Join-Path $env:USERPROFILE '.fallow'
$ThreadEnv  = 'LLAMA_ARG_THREADS'

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
    Write-Log "stopping and unregistering $TaskName  (untested - verify on target)"
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
}

Stop-FallowProcesses

if ($null -ne [Environment]::GetEnvironmentVariable($ThreadEnv, 'User')) {
    if ($PSCmdlet.ShouldProcess("user environment $ThreadEnv", 'clear')) {
        [Environment]::SetEnvironmentVariable($ThreadEnv, $null, 'User')
        Write-Log "cleared $ThreadEnv from the pilot account"
    }
}

if ($Purge) {
    if ($PSCmdlet.ShouldProcess($FallowHome, 'delete per-user state')) {
        Remove-Item -Recurse -Force $FallowHome -ErrorAction SilentlyContinue
        Write-Log "purged $FallowHome"
    }
} else {
    Write-Log "preserved $FallowHome (config, models, logs); re-run with -Purge to delete it"
}

Write-Log 'done'
