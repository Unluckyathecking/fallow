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

    The install is idempotent: re-running drops any previous task registration
    first and never clobbers a live config. On a machine with no NVIDIA GPU it
    caps CPU threads (LLAMA_ARG_THREADS) so the CPU llama.cpp build stays polite
    on a shared box; uninstall.ps1 removes that variable again.

    Dry runs leave nothing behind. -WhatIf walks the whole install with no side
    effects (the acceptance harness uses this). -DryRun prints the rendered task
    XML and exits before touching anything.

    Prerequisites (see deploy\README.md):
      - Python flavour: a git checkout of the fallow repo + uv (https://docs.astral.sh/uv/)
      - Go flavour: a prebuilt agentctl.exe (a GitHub Release archive, or `go build`)
      - Both: Tailscale up; agent config binds replicas to the tailnet IP; the
        right llama.cpp build staged under deploy\bin\windows\ (run
        deploy\windows\fetch-llama.ps1 first); Defender / SmartScreen
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
    Print the rendered task XML and exit before touching the system. Used by the
    render test. For a full no-side-effect walk use -WhatIf instead.
#>
[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'Medium')]
param(
    [string]$RepoRoot,
    [string]$GoBinary,
    [string]$JoinBundle,
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Log { param([string]$Message) Write-Host "[install] $Message" }
function Throw-Err { param([string]$Message) throw "[install] ERROR: $Message" }

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$DeployDir = Split-Path -Parent $ScriptDir
$DefaultRepo = Split-Path -Parent $DeployDir

. (Join-Path $ScriptDir 'lib\backend.ps1')
. (Join-Path $ScriptDir 'new-site-config.ps1')

$TaskName    = 'Fallow\FallowAgent'
# Get/Unregister/Stop-ScheduledTask resolve by leaf name + folder, not the
# combined 'Fallow\FallowAgent' string (which Register/Start accept). Keep the
# split form so the idempotent pre-drop and uninstall actually find the task.
$TaskLeaf    = 'FallowAgent'
$TaskFolder  = '\Fallow\'
$FallowHome  = Join-Path $env:USERPROFILE '.fallow'
$LogDir      = Join-Path $FallowHome 'logs'
$ConfigDst   = Join-Path $FallowHome 'agent.toml'
$ConfigSrc   = Join-Path $DeployDir 'agent.example.toml'    # created by the config module (I2)
$XmlTemplate = Join-Path $ScriptDir 'fallow-agent-task.xml'
# The canonical COMPUTERNAME\user (or DOMAIN\user) form. $env:USERDOMAIN is
# "WORKGROUP" on a workgroup machine, which icacls cannot map to a SID; the
# WindowsIdentity name is correct there and for a domain join, and is also the
# right principal for the task's LogonTrigger.
$UserId      = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$BinDir      = Join-Path $FallowHome 'bin'
$AgentBin    = Join-Path $BinDir 'agentctl.exe'
$ThreadEnv   = 'LLAMA_ARG_THREADS'
$SiteStateDir = Join-Path $FallowHome 'site'
$SiteJoinDst  = Join-Path $SiteStateDir 'join.json'

if (-not (Test-Path $XmlTemplate)) { Throw-Err "missing task template $XmlTemplate" }

# An explicit but empty -GoBinary clearly meant the Go flavour; reject it rather
# than silently falling through to the Python install.
if ($PSBoundParameters.ContainsKey('GoBinary') -and [string]::IsNullOrEmpty($GoBinary)) {
    Throw-Err '-GoBinary requires a non-empty path'
}
if ($PSBoundParameters.ContainsKey('JoinBundle') -and [string]::IsNullOrEmpty($JoinBundle)) {
    Throw-Err '-JoinBundle requires a non-empty path'
}
if ($JoinBundle -and -not $GoBinary) {
    Throw-Err 'Site Mode requires -GoBinary; the Python agent does not implement Site Mode'
}

# Validate the sensitive artifact before creating directories, copying a binary,
# changing a config, or touching Task Scheduler. Do not log its contents.
$SiteJoin = $null
$SiteLlama = $null
if ($JoinBundle) {
    $SiteJoin = Read-FallowSiteJoin -Path $JoinBundle

    # Preflight the persisted identity before any Site side effect. An enrolled
    # Site agent must keep its identity: the runtime would ignore the new join
    # and leave a live token on disk, so skip the bundle and re-install only the
    # program and task. A non-Site identity cannot be converted, so reject it
    # before copying a binary or rewriting the config.
    $statePath = Read-FallowConfigValue -ConfigPath $ConfigDst -Key 'state_path'
    if ($statePath) { $statePath = Expand-FallowHome $statePath } else { $statePath = Join-Path $FallowHome 'agent-state.json' }
    switch (Get-FallowInstallDisposition -StatePath $statePath) {
        'site' {
            Write-Log "an enrolled Site identity already exists at $statePath; keeping it and skipping the join bundle (re-installing the program and task only)"
            $SiteJoin = $null
        }
        'fresh' {
            # Fail loudly now if the Windows llama-server is not staged: a Site
            # config that keeps the example's Unix path would let agentctl doctor
            # fail and the agent never serve.
            $SiteLlama = Resolve-FallowStagedLlama -DeployDir $DeployDir
            if (-not $SiteLlama) {
                Throw-Err "no staged llama-server.exe under $(Join-Path $DeployDir 'bin\windows'); run deploy\windows\fetch-llama.ps1 first"
            }
        }
    }
}

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

# -- Backend: pick the build the machine can actually use --------------------
# fetch-llama.ps1 stages the matching binary; here we only report it and, on the
# CPU fallback, cap llama-server threads so the box stays usable.
$backend = Get-FallowBackend
Write-Log "backend: $backend"
if ($backend -eq 'cpu') {
    $threads = Get-FallowCpuThreadLimit
    if ($PSCmdlet.ShouldProcess("user environment $ThreadEnv", "set to $threads")) {
        [Environment]::SetEnvironmentVariable($ThreadEnv, "$threads", 'User')
        Write-Log "CPU build: capped $ThreadEnv=$threads for the pilot account"
    }
} else {
    Write-Log 'NVIDIA GPU detected: using the CUDA build, no CPU thread cap'
}

if ($PSCmdlet.ShouldProcess("$FallowHome, $LogDir", 'create directories')) {
    New-Item -ItemType Directory -Force -Path $FallowHome, $LogDir | Out-Null
}

# -- Install the agent program ------------------------------------------------
if ($GoBinary) {
    if ($PSCmdlet.ShouldProcess($AgentBin, 'install Go agent binary')) {
        Write-Log "installing Go agent binary -> $AgentBin  (untested - verify on target)"
        New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
        Copy-Item $GoBinary $AgentBin -Force
    }
} else {
    if ($PSCmdlet.ShouldProcess($RepoRoot, 'uv python install + uv sync')) {
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
}

# -- config: legacy installs retain their current first-install behaviour ------
if ($SiteJoin) {
    if ($PSCmdlet.ShouldProcess($SiteStateDir, 'install protected Site Mode join file')) {
        New-Item -ItemType Directory -Force -Path $SiteStateDir | Out-Null
        Protect-FallowSitePath -Path $SiteStateDir -UserId $UserId -Directory
        Copy-Item -LiteralPath $JoinBundle -Destination $SiteJoinDst -Force
        Protect-FallowSitePath -Path $SiteJoinDst -UserId $UserId

        # Seed the example on first install so the required non-Site keys (notably
        # llama_server_binary) are present; a re-run keeps the live config and the
        # operator's edits. Write-FallowSiteConfig then owns only the Site fields.
        if (-not (Test-Path $ConfigDst) -and (Test-Path $ConfigSrc)) {
            Copy-Item $ConfigSrc $ConfigDst
        }

        # A Site install replaces legacy URL/token fields with the join reference
        # and loopback bind, and points llama_server_binary at the staged Windows
        # build resolved above. The join copy is consumed and made token-free by
        # the Go agent after successful enrollment.
        Write-FallowSiteConfig -ConfigPath $ConfigDst -JoinBundlePath $SiteJoinDst -LlamaServerBinary $SiteLlama
        Protect-FallowSitePath -Path $ConfigDst -UserId $UserId
        Write-Log "installed protected Site Mode join file and token-free config (llama_server_binary=$SiteLlama)"
        $llamaPath = Read-FallowConfigValue -ConfigPath $ConfigDst -Key 'llama_server_binary'
        if (-not $llamaPath -or -not (Test-Path -LiteralPath $llamaPath -PathType Leaf)) {
            Throw-Err "rendered llama_server_binary '$llamaPath' does not point at a file; the agent cannot serve"
        }
    }
} elseif (Test-Path $ConfigDst) {
    Write-Log "keeping existing config $ConfigDst"
} elseif (Test-Path $ConfigSrc) {
    if ($PSCmdlet.ShouldProcess($ConfigDst, 'copy example config')) {
        Copy-Item $ConfigSrc $ConfigDst
        Write-Log "copied example config -> $ConfigDst (EDIT IT: enrollment token, coordinator URL, tailnet bind_host, llama_server_binary path)"
    }
} else {
    Write-Log "WARNING: no config at $ConfigDst and no example at $ConfigSrc; create it before the agent will start"
}

# -- register the scheduled task ---------------------------------------------
if ($PSCmdlet.ShouldProcess($TaskName, 'register at-logon scheduled task')) {
    Write-Log "registering scheduled task $TaskName"
    # Register-ScheduledTask hands the scheduler a .NET (UTF-16) string; a UTF-8
    # encoding declaration in the prolog makes its parser fail with "unable to
    # switch the encoding". Drop the encoding attribute so the string registers.
    $taskXml = $xml -replace '<\?xml version="1\.0" encoding="[^"]*"\?>', '<?xml version="1.0"?>'
    # Idempotent re-install: drop any previous registration first.
    Unregister-ScheduledTask -TaskName $TaskLeaf -TaskPath $TaskFolder -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName $TaskName -Xml $taskXml -Force | Out-Null
    Write-Log 'registered  (untested - verify on target)'
}

# Start it now so the user does not have to log out/in for first run.
if ($PSCmdlet.ShouldProcess($TaskName, 'start now')) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Log "started. inspect: Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo"
}
Write-Log 'uninstall: deploy\windows\uninstall.ps1'
