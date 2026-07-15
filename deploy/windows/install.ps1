<#
.SYNOPSIS
    Install the Fallow agent as an at-logon Scheduled Task in the user session
    on Windows.

.DESCRIPTION
    v0.1 install story: Fallow is NOT on PyPI, so this assumes a git checkout of
    the fallow monorepo already exists on the machine. It bootstraps a
    standalone CPython via `uv python install`, creates a uv-managed venv in the
    checkout (`uv sync --no-dev`), templates fallow-agent-task.xml with the
    resolved pythonw.exe / config / working dir, and registers it as an at-logon
    task running in the user's session (see the XML comment for why it must be a
    task, not a service).

    Prerequisites (see deploy\README.md):
      - a git checkout of the fallow repo (pass -RepoRoot or set FALLOW_REPO)
      - uv installed (https://docs.astral.sh/uv/)
      - Tailscale up; agent config binds replicas to the tailnet IP
      - deploy\bin\windows\llama-server.exe + cudart DLLs present
        (run deploy\windows\fetch-llama.ps1 first)
      - Defender / SmartScreen allowlisting arranged (see README; org lead time)

    HONESTY: authored in a sandbox with no Windows host. The uv bootstrap and
    Register-ScheduledTask steps are marked (untested - verify on target).

.PARAMETER RepoRoot
    Path to the fallow git checkout. Defaults to $env:FALLOW_REPO, then to the
    repo this script lives in.
#>
[CmdletBinding()]
param(
    [string]$RepoRoot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Log { param([string]$Message) Write-Host "[install] $Message" }
function Throw-Err { param([string]$Message) throw "[install] ERROR: $Message" }

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$DeployDir = Split-Path -Parent $ScriptDir
$DefaultRepo = Split-Path -Parent $DeployDir

if (-not $RepoRoot) { $RepoRoot = $env:FALLOW_REPO }
if (-not $RepoRoot) { $RepoRoot = $DefaultRepo }
if (-not (Test-Path (Join-Path $RepoRoot 'pyproject.toml'))) {
    Throw-Err "no pyproject.toml at $RepoRoot; pass -RepoRoot <fallow checkout>"
}

$TaskName    = 'Fallow\FallowAgent'
$FallowHome  = Join-Path $env:USERPROFILE '.fallow'
$LogDir      = Join-Path $FallowHome 'logs'
$ConfigDst   = Join-Path $FallowHome 'agent.toml'
$ConfigSrc   = Join-Path $DeployDir 'agent.example.toml'    # created by the config module (I2)
$XmlTemplate = Join-Path $ScriptDir 'fallow-agent-task.xml'
$UserId      = "$env:USERDOMAIN\$env:USERNAME"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Throw-Err 'uv is required (https://docs.astral.sh/uv/)'
}
if (-not (Test-Path $XmlTemplate)) { Throw-Err "missing task template $XmlTemplate" }

New-Item -ItemType Directory -Force -Path $FallowHome, $LogDir | Out-Null

# -- Standalone Python + venv via uv -----------------------------------------
Write-Log 'bootstrapping standalone CPython via uv  (untested - verify on target)'
Push-Location $RepoRoot
try {
    & uv python install 3.12
    & uv sync --no-dev
} finally {
    Pop-Location
}

# pythonw.exe = windowless interpreter (no flashing console at logon).
$PythonW = Join-Path $RepoRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $PythonW)) {
    Throw-Err "expected venv pythonw at $PythonW after 'uv sync'"
}

# -- config: copy example on first install, never clobber a live one ----------
if (Test-Path $ConfigDst) {
    Write-Log "keeping existing config $ConfigDst"
} elseif (Test-Path $ConfigSrc) {
    Copy-Item $ConfigSrc $ConfigDst
    Write-Log "copied example config -> $ConfigDst (EDIT IT: enrollment token, coordinator URL, tailnet bind_host, llama_binary path)"
} else {
    Write-Log "WARNING: no config at $ConfigDst and no example at $ConfigSrc; create it before the agent will start"
}

# -- render the task XML template --------------------------------------------
Write-Log "registering scheduled task $TaskName"
$xml = Get-Content -Raw -Path $XmlTemplate
$xml = $xml.Replace('__USERID__',  $UserId)
$xml = $xml.Replace('__PYTHONW__', $PythonW)
$xml = $xml.Replace('__CONFIG__',  $ConfigDst)
$xml = $xml.Replace('__WORKDIR__', $RepoRoot)

# Idempotent re-install: drop any previous registration first.
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask -TaskName $TaskName -Xml $xml -Force | Out-Null
Write-Log 'registered  (untested - verify on target)'

# Start it now so the user does not have to log out/in for first run.
Start-ScheduledTask -TaskName $TaskName
Write-Log "started. inspect: Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo"
Write-Log 'uninstall: deploy\windows\uninstall.ps1'
