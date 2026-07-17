<#
.SYNOPSIS
    Turn a fresh Windows machine into an enrolled Fallow agent in one command.

.DESCRIPTION
    A thin orchestrator, not a second installer. It reads the machine (OS, CPU
    arch, RAM, GPU), picks the llama.cpp backend, then hands off to the hardened
    installer (deploy\windows\install.ps1) with the matching flavour. All the
    real work — the uv/venv build, SHA256 verification against the signed
    manifest, Scheduled Task wiring — stays in install.ps1. Nothing is
    duplicated or relaxed here.

    Windows only. macOS uses deploy/bootstrap.sh; the two are siblings, not one
    cross-platform script, because the service managers (Task Scheduler vs
    launchd) share no plumbing worth abstracting.

    Enrollment token: pass -Token <t> or set FALLOW_ENROLLMENT_TOKEN. The token
    is held in memory only. It is fed to a single foreground enrollment run
    through that process's environment, never written to a file, and cleared
    once the agent has registered. The agent persists its identity, not the
    token, so nothing secret survives on disk (see ADR 062).

    Backend: NVIDIA -> CUDA, otherwise CPU. The shipped Windows llama.cpp build
    is CUDA-only (deploy\windows\fetch-llama.ps1), so a machine with no NVIDIA
    GPU is detected and warned rather than silently misconfigured.

    Dry run: -WhatIf reports the detection result and delegates to install.ps1's
    own -DryRun preview. It touches nothing — no uv, no Task Scheduler, no
    enrollment, no self-test. This is the path the acceptance harness drives.

    HONESTY: authored in a sandbox with no Windows host. The install,
    enrollment, and self-test steps reach Task Scheduler and the network and are
    marked (untested - verify on target).

.PARAMETER Token
    One-time enrollment token. Defaults to $env:FALLOW_ENROLLMENT_TOKEN.

.PARAMETER RepoRoot
    Path to the fallow checkout for the Python flavour. Passed straight through
    to install.ps1, which defaults it when empty.

.PARAMETER GoBinary
    Path to a prebuilt agentctl.exe. Installs the Go agent and skips the venv.

.PARAMETER WhatIf
    Report detection and delegation, change nothing.
#>
[CmdletBinding()]
param(
    [string]$Token = $env:FALLOW_ENROLLMENT_TOKEN,
    [string]$RepoRoot,
    [string]$GoBinary,
    [switch]$WhatIf
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Log  { param([string]$Message) Write-Host "[bootstrap] $Message" }
function Write-Warn { param([string]$Message) Write-Warning "[bootstrap] $Message" }
function Stop-Err   { param([string]$Message) throw "[bootstrap] ERROR: $Message" }

$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$Installer  = Join-Path $ScriptDir 'windows\install.ps1'
$TaskName   = 'Fallow\FallowAgent'
$FallowHome = Join-Path $env:USERPROFILE '.fallow'
$StateFile  = Join-Path $FallowHome 'agent-state.json'
$ConfigFile = Join-Path $FallowHome 'agent.toml'

# $IsWindows exists only on PowerShell 6+, so gate on the version first: the
# -and short-circuits on 5.1 (Windows-only) before the variable is referenced.
if (($PSVersionTable.PSVersion.Major -ge 6) -and (-not $IsWindows)) {
    Stop-Err 'bootstrap.ps1 is Windows-only; on macOS run deploy/bootstrap.sh'
}
if (-not (Test-Path $Installer)) { Stop-Err "missing installer $Installer" }

# -- Detect the machine -------------------------------------------------------
$Arch  = $env:PROCESSOR_ARCHITECTURE
$RamGb = [math]::Floor((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)

# NVIDIA present if nvidia-smi resolves or a video controller reports NVIDIA.
$HasNvidia = [bool](Get-Command nvidia-smi -ErrorAction SilentlyContinue)
if (-not $HasNvidia) {
    $HasNvidia = [bool](Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue |
                        Where-Object { $_.Name -match 'NVIDIA' })
}
if ($HasNvidia) { $Gpu = 'NVIDIA'; $Backend = 'cuda' } else { $Gpu = 'none'; $Backend = 'cpu' }

Write-Log "os=Windows arch=$Arch ram=${RamGb}GB gpu=$Gpu -> backend=$Backend"
if ($RamGb -lt 8) { Write-Warn "only ${RamGb}GB RAM; the agent may be starved on a shared machine" }
if ($Backend -eq 'cpu') {
    Write-Warn 'no NVIDIA GPU; the shipped Windows llama.cpp build is CUDA-only (see deploy\windows\fetch-llama.ps1)'
}

# -- Build the install.ps1 argument set (flavour) -----------------------------
$InstallArgs = @{}
if ($GoBinary)  { $InstallArgs['GoBinary'] = $GoBinary }
if ($RepoRoot)  { $InstallArgs['RepoRoot'] = $RepoRoot }

if ($WhatIf) {
    Write-Log 'dry run: delegating to install.ps1 preview (no side effects)'
    & $Installer @InstallArgs -DryRun | Out-Null
    Write-Log 'dry run OK: detection and delegation exercised, nothing changed'
    return
}

Write-Log "installing (backend=$Backend); install.ps1 verifies every binary before it runs"
& $Installer @InstallArgs

# -- Enrollment ---------------------------------------------------------------
# One-time token fed to a single foreground enrollment run via that process's
# environment (in memory), then cleared. The agent registers once, persists its
# identity, and the Scheduled Task loads that identity on every later start.
function Wait-ForIdentity {
    for ($i = 0; $i -lt 60; $i++) {
        if (Test-Path $StateFile) { return $true }
        Start-Sleep -Seconds 1
    }
    return $false
}

if ($Token) {
    if (Test-Path $StateFile) {
        Write-Log "already enrolled ($StateFile exists); ignoring the supplied token"
    } else {
        # Mirror the arg vector install.ps1 wired, run it once to register.
        if ($GoBinary) {
            $program = Join-Path $FallowHome 'bin\agentctl.exe'
            $runArgs = @('run', '-config', $ConfigFile)
        } else {
            if (-not $RepoRoot) { $RepoRoot = Split-Path -Parent $ScriptDir }
            $program = Join-Path $RepoRoot '.venv\Scripts\python.exe'
            $runArgs = @('-m', 'fallow_agent', 'run', '--config', $ConfigFile)
        }
        Write-Log 'enrolling via one-time token (kept in memory, never written to disk)'
        $env:FALLOW_ENROLLMENT_TOKEN = $Token
        try {
            $proc = Start-Process -FilePath $program -ArgumentList $runArgs -PassThru -NoNewWindow
            if (Wait-ForIdentity) {
                Write-Log "enrolled: identity persisted at $StateFile"
            } else {
                Write-Warn "no identity after 60s; check $FallowHome\logs"
            }
            if ($proc -and -not $proc.HasExited) { Stop-Process -Id $proc.Id -Force }
        } finally {
            Remove-Item Env:FALLOW_ENROLLMENT_TOKEN -ErrorAction SilentlyContinue
        }
    }
    # Belt and braces: the token must never have landed in the config file.
    if ((Test-Path $ConfigFile) -and (Select-String -Path $ConfigFile -SimpleMatch -Pattern $Token -Quiet)) {
        Stop-Err "enrollment token found in $ConfigFile; refusing to leave a secret on disk"
    }
} else {
    Write-Log 'no enrollment token given; the agent will not register until one is supplied'
}

# -- Self-test ----------------------------------------------------------------
# Report observable post-install state without touching the network: the
# Scheduled Task is registered and the config is in place.
$ok = $true
if (Get-ScheduledTask -TaskName 'FallowAgent' -TaskPath '\Fallow\' -ErrorAction SilentlyContinue) {
    Write-Log "self-test: Scheduled Task $TaskName is registered"
} else {
    Write-Warn "self-test: Scheduled Task $TaskName is not registered"; $ok = $false
}
if (Test-Path $ConfigFile) {
    Write-Log "self-test: config present at $ConfigFile"
} else {
    Write-Warn "self-test: no config at $ConfigFile"; $ok = $false
}

if (-not $ok) { Stop-Err 'self-test failed; see warnings above' }
Write-Log "self-test passed; inspect: Get-ScheduledTask -TaskName '$TaskName'"
