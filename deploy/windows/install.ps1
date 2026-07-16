<#
.SYNOPSIS
    Install the Fallow agent as an at-logon Scheduled Task in the user session
    on Windows.

.DESCRIPTION
    Two flavours share the same Scheduled Task, config, and registration wiring:

      1. Python agent (default). Fallow is NOT on PyPI, so this assumes a git
         checkout of the fallow monorepo exists on the machine. It bootstraps a
         standalone CPython via `uv python install`, creates a uv-managed venv in
         the checkout (`uv sync --no-dev`), and runs `pythonw -m fallow_agent run`.

      2. Prebuilt Go binary (-GoBinary <path>). Point the task at a released
         agentctl.exe instead. This skips uv/venv entirely: it copies the binary
         into %USERPROFILE%\.fallow\bin and wires the task to `agentctl run`.

    Prerequisites (see deploy\README.md):
      - Python flavour: a git checkout of the fallow repo + uv (https://docs.astral.sh/uv/)
      - Go flavour: a prebuilt agentctl.exe (a GitHub Release archive, or `go build`)
      - Both: Tailscale up; agent config binds replicas to the tailnet IP;
        deploy\bin\windows\llama-server.exe + cudart DLLs present
        (run deploy\windows\fetch-llama.ps1 first); Defender / SmartScreen
        allowlisting arranged (see README; org lead time)

    HONESTY: authored in a sandbox with no Windows host. The uv bootstrap,
    binary install, and Register-ScheduledTask steps are marked (untested -
    verify on target).

.PARAMETER RepoRoot
    Path to the fallow git checkout (Python flavour). Defaults to $env:FALLOW_REPO,
    then to the repo this script lives in.

.PARAMETER GoBinary
    Path to a prebuilt agentctl.exe. When given, installs the Go agent and skips
    the uv/venv Python setup.

.PARAMETER DryRun
    Print the rendered task XML and exit before touching the system (uv, the
    binary copy, Task Scheduler). Used by the render test.
#>
[CmdletBinding()]
param(
    [string]$RepoRoot,
    [string]$GoBinary,
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Log { param([string]$Message) Write-Host "[install] $Message" }
function Throw-Err { param([string]$Message) throw "[install] ERROR: $Message" }

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$DeployDir = Split-Path -Parent $ScriptDir
$DefaultRepo = Split-Path -Parent $DeployDir

$TaskName    = 'Fallow\FallowAgent'
$FallowHome  = Join-Path $env:USERPROFILE '.fallow'
$LogDir      = Join-Path $FallowHome 'logs'
$ConfigDst   = Join-Path $FallowHome 'agent.toml'
$ConfigSrc   = Join-Path $DeployDir 'agent.example.toml'    # created by the config module (I2)
$XmlTemplate = Join-Path $ScriptDir 'fallow-agent-task.xml'
$UserId      = "$env:USERDOMAIN\$env:USERNAME"
$BinDir      = Join-Path $FallowHome 'bin'
$AgentBin    = Join-Path $BinDir 'agentctl.exe'

if (-not (Test-Path $XmlTemplate)) { Throw-Err "missing task template $XmlTemplate" }

# -- Select the agent flavour -------------------------------------------------
# $ProgramPath / $WorkDir are the only per-flavour differences the task needs;
# the Go path additionally rewrites the arg vector at render time (see below).
if ($GoBinary) {
    if (-not (Test-Path $GoBinary)) { Throw-Err "no binary at $GoBinary" }
    $ProgramPath = $AgentBin
    $WorkDir     = $FallowHome
} else {
    if (-not $RepoRoot) { $RepoRoot = $env:FALLOW_REPO }
    if (-not $RepoRoot) { $RepoRoot = $DefaultRepo }
    if (-not (Test-Path (Join-Path $RepoRoot 'pyproject.toml'))) {
        Throw-Err "no pyproject.toml at $RepoRoot; pass -RepoRoot <fallow checkout>"
    }
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Throw-Err 'uv is required (https://docs.astral.sh/uv/)'
    }
    # pythonw.exe = windowless interpreter (no flashing console at logon).
    $ProgramPath = Join-Path $RepoRoot '.venv\Scripts\pythonw.exe'
    $WorkDir     = $RepoRoot
}

# -- Render the task XML template --------------------------------------------
# The template ships the Python arg vector (`-m fallow_agent run --config`). For
# the Go flavour we drop the `-m fallow_agent` interpreter args and switch to the
# binary's single-dash `-config`, leaving `agentctl run -config "<path>"`. This
# keeps the task XML single-sourced and Python-shaped on disk.
$xml = Get-Content -Raw -Path $XmlTemplate
$xml = $xml.Replace('__USERID__',  $UserId)
$xml = $xml.Replace('__PYTHONW__', $ProgramPath)
if ($GoBinary) {
    $xml = $xml.Replace('-m fallow_agent run --config', 'run -config')
}
$xml = $xml.Replace('__CONFIG__',  $ConfigDst)
$xml = $xml.Replace('__WORKDIR__', $WorkDir)

if ($DryRun) { Write-Output $xml; return }

New-Item -ItemType Directory -Force -Path $FallowHome, $LogDir | Out-Null

# -- Install the agent program ------------------------------------------------
if ($GoBinary) {
    Write-Log "installing Go agent binary -> $AgentBin  (untested - verify on target)"
    New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
    Copy-Item $GoBinary $AgentBin -Force
} else {
    Write-Log 'bootstrapping standalone CPython via uv  (untested - verify on target)'
    Push-Location $RepoRoot
    try {
        & uv python install 3.12
        & uv sync --no-dev
    } finally {
        Pop-Location
    }
    if (-not (Test-Path $ProgramPath)) {
        Throw-Err "expected venv pythonw at $ProgramPath after 'uv sync'"
    }
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

# -- register the scheduled task ---------------------------------------------
Write-Log "registering scheduled task $TaskName"

# Idempotent re-install: drop any previous registration first.
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask -TaskName $TaskName -Xml $xml -Force | Out-Null
Write-Log 'registered  (untested - verify on target)'

# Start it now so the user does not have to log out/in for first run.
Start-ScheduledTask -TaskName $TaskName
Write-Log "started. inspect: Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo"
Write-Log 'uninstall: deploy\windows\uninstall.ps1'
