<#
.SYNOPSIS
    Remove the Fallow agent Scheduled Task on Windows.

.DESCRIPTION
    Stops and unregisters the at-logon task. By default PRESERVES
    %USERPROFILE%\.fallow (config, model cache, logs). Pass -Purge to delete it.
    Never touches the git checkout or deploy\bin.

.PARAMETER Purge
    Also delete %USERPROFILE%\.fallow.
#>
[CmdletBinding()]
param(
    [switch]$Purge
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Log { param([string]$Message) Write-Host "[uninstall] $Message" }

$TaskName   = 'Fallow\FallowAgent'
$FallowHome = Join-Path $env:USERPROFILE '.fallow'

Write-Log "stopping and unregistering $TaskName  (untested - verify on target)"
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

if ($Purge) {
    Remove-Item -Recurse -Force $FallowHome -ErrorAction SilentlyContinue
    Write-Log "purged $FallowHome"
} else {
    Write-Log "preserved $FallowHome (config, models, logs); re-run with -Purge to delete it"
}

Write-Log 'done'
