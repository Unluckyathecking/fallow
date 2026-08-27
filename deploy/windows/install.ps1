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

    Either flavour installs for the account running the script. -User <name>
    installs for a nominated account instead, from an elevated admin or SYSTEM
    context, for a fleet deployed by MDM rather than by walking to each desk. The
    agent itself still runs as an at-logon task in that account's interactive
    session: only the registration moves. See ..\README.md beside this script in
    the desk bundle, or docs\pilot\remote-install.md in the repository.

    The install is idempotent: re-running drops any previous task registration
    first and never clobbers a live config. On a machine with no NVIDIA GPU it
    caps CPU threads (LLAMA_ARG_THREADS) so the CPU llama.cpp build stays polite
    on a shared box; uninstall.ps1 removes that variable again.

    Dry runs leave nothing behind. -WhatIf walks the whole install with no side
    effects (the acceptance harness uses this). -DryRun prints the rendered task
    XML and exits before touching anything.

    Prerequisites (see ..\README.md in the desk bundle, or deploy\README.md in
    the repository):
      - Python flavour: a git checkout of the fallow repo + uv (https://docs.astral.sh/uv/)
      - Go flavour: a prebuilt agentctl.exe (a GitHub Release archive, or `go build`)
      - Both: Tailscale up; agent config binds replicas to the tailnet IP; the
        right llama.cpp build staged under deploy\bin\windows\ (run
        deploy\windows\fetch-llama.ps1 first); Defender / SmartScreen
        allowlisting arranged (see README; org lead time)

    HONESTY: authored in a sandbox with no Windows host. The binary install and
    Register-ScheduledTask steps now run for real on windows-latest in
    .github\workflows\install-acceptance.yml, so they are marked (exercised in CI
    on windows-latest - verify on target). The uv bootstrap is not, and no runner
    proves the registered task starts at a real logon; both stay (untested -
    verify on target).

.PARAMETER RepoRoot
    Path to the fallow git checkout (Python flavour). Defaults to $env:FALLOW_REPO,
    then to the repo this script lives in.

.PARAMETER GoBinary
    Path to a prebuilt agentctl.exe. When given, installs the Go agent and skips
    the uv/venv Python setup.

.PARAMETER User
    Install for another account, from an elevated admin or SYSTEM context
    (Intune, ConfigMgr, PDQ, a GPO startup script) instead of walking to the
    desk. Files are staged under that account's profile and the at-logon task is
    registered for it; the agent still runs in that account's own interactive
    session at its next logon. Requires elevation and -GoBinary, and refuses an
    account that has never signed in to this machine. See ..\README.md in the
    desk bundle, or docs\pilot\remote-install.md in the repository.

.PARAMETER DryRun
    Print the rendered task XML and exit before touching the system. Used by the
    render test. For a full no-side-effect walk use -WhatIf instead.
#>
[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'Medium')]
param(
    [string]$RepoRoot,
    [string]$GoBinary,
    [string]$JoinBundle,
    [string]$User,
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
. (Join-Path $ScriptDir 'lib\target-user.ps1')
. (Join-Path $ScriptDir 'new-site-config.ps1')

# -- Which account is this install for? ---------------------------------------
# Without -User: this session's own account, the walk-to-the-desk path, byte for
# byte what it always did. With -User: a nominated account, from an elevated
# admin or SYSTEM context. Only three things differ - the profile the files land
# in, the identity on the ACLs and the task, and the environment hive - so
# resolve them here and let the rest of the script read them.
if ($PSBoundParameters.ContainsKey('User') -and [string]::IsNullOrEmpty($User)) {
    Throw-Err '-User requires a non-empty account name'
}
if ($User) {
    if (-not $GoBinary) {
        Throw-Err '-User requires -GoBinary; the Python flavour bootstraps a venv in the installing account''s context, which is the wrong account here'
    }
    if (-not (Test-FallowElevated)) {
        Throw-Err "-User installs into another account's profile and registers a task for it; run it elevated (an admin shell, or SYSTEM under Intune/ConfigMgr/PDQ/GPO)"
    }
    $Target      = Resolve-FallowTargetUser -Name $User
    $UserId      = $Target.Name
    $UserSid     = $Target.Sid
    $UserProfile = $Target.ProfilePath
    Write-Log "admin context: installing for $UserId ($UserProfile)"
    if (-not (Test-FallowUserHiveLoaded -Sid $UserSid)) {
        Write-Log "note: $UserId is signed out, so their FALLOW_* environment overrides cannot be read or written from here; deploy\windows\doctor.ps1 in their session reports the result of first logon"
    }
} else {
    # The canonical COMPUTERNAME\user (or DOMAIN\user) form. $env:USERDOMAIN is
    # "WORKGROUP" on a workgroup machine, which icacls cannot map to a SID; the
    # WindowsIdentity name is correct there and for a domain join, and is also
    # the right principal for the task's LogonTrigger.
    $UserId      = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $UserSid     = $null
    $UserProfile = $env:USERPROFILE
}

# FALLOW_* overrides as the account the task will run as sees them. In admin
# context that is the target's hive plus the machine scope; this process's own
# User and Process values belong to another account and must not count.
function Get-FallowInstallEnv {
    param([Parameter(Mandatory)][string]$Name)
    if (-not $UserSid) { return (Get-FallowPersistedEnv $Name) }
    $value = Get-FallowTargetEnv -Sid $UserSid -Name $Name
    if (-not [string]::IsNullOrEmpty($value)) { return $value }
    $machine = [Environment]::GetEnvironmentVariable($Name, 'Machine')
    if (-not [string]::IsNullOrEmpty($machine)) { return $machine }
    return $null
}

$TaskName    = 'Fallow\FallowAgent'
# Get/Unregister/Stop-ScheduledTask resolve by leaf name + folder, not the
# combined 'Fallow\FallowAgent' string (which Register/Start accept). Keep the
# split form so the idempotent pre-drop and uninstall actually find the task.
$TaskLeaf    = 'FallowAgent'
$TaskFolder  = '\Fallow\'
$FallowHome  = Join-Path $UserProfile '.fallow'
$LogDir      = Join-Path $FallowHome 'logs'
$ConfigDst   = Join-Path $FallowHome 'agent.toml'
$ConfigSrc   = Join-Path $DeployDir 'agent.example.toml'    # created by the config module (I2)
$XmlTemplate = Join-Path $ScriptDir 'fallow-agent-task.xml'
$BinDir      = Join-Path $FallowHome 'bin'
$AgentBin    = Join-Path $BinDir 'agentctl.exe'
$ThreadEnv   = 'LLAMA_ARG_THREADS'
$SiteStateDir = Join-Path $FallowHome 'site'
$SiteJoinDst  = Join-Path $SiteStateDir 'join.json'
# join.json holds the single-use token and is granted to the task user alone in
# either mode. The two containers around it are not the token: an admin install
# has to write into .fallow\site and read back the agent.toml it wrote on a
# re-run, from an account that is not the task user, so there they also grant the
# machine's own trust principals - which already hold everything else in the
# profile, including the persisted device token, by inheritance. Granting the
# directory does not grant the file: join.json keeps its own protected DACL.
$AdminAlsoAllow = @()
if ($User) { $AdminAlsoAllow = @('BUILTIN\Administrators', 'NT AUTHORITY\SYSTEM') }

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
# An install made in the target's own session leaves an owner-only agent.toml
# that this account cannot read. Every path below reads that config to decide
# what to keep, so refuse now with the remedy rather than fail mid-install or,
# worse, clobber a live config the installer could not inspect.
if ($User -and (Test-Path $ConfigDst) -and -not (Test-FallowPathReadable -Path $ConfigDst)) {
    Throw-Err "$ConfigDst exists but this account cannot read it, so it was installed from $UserId's own session. Re-run there, or remove that install first with uninstall.ps1 -User '$User' -Purge"
}
# The same for the Site state directory, which the same old-style install leaves
# granted to the task user alone. Protect-FallowSitePath opens with Get-Acl,
# which needs READ_CONTROL this account does not hold there; refuse with the
# remedy rather than fail inside it.
if ($User -and (Test-Path $SiteStateDir) -and -not (Test-FallowPathReadable -Path $SiteStateDir)) {
    Throw-Err "$SiteStateDir exists but this account cannot read it, so it was installed from $UserId's own session. Re-run there, or remove that install first with uninstall.ps1 -User '$User' -Purge"
}

# Validate the sensitive artifact before creating directories, copying a binary,
# changing a config, or touching Task Scheduler. Do not log its contents.
$SiteJoin = $null
$SiteLlama = $null
$SiteResume = $false
$SiteNeedsConfig = $false
$SiteStatePath = $null
$SiteRevokedMarker = $null
if ($JoinBundle) {
    $SiteJoin = Read-FallowSiteJoin -Path $JoinBundle

    # Preflight the persisted identity before any Site side effect. An enrolled
    # Site agent must keep its identity: the runtime would ignore the new join
    # and leave a live token on disk, so skip the token bundle and re-install
    # only the program, task, and (if needed) a token-free Site config. A
    # non-Site identity cannot be converted, so reject it before copying a
    # binary or rewriting the config.
    # FALLOW_STATE_PATH > TOML state_path > default, matching the Go config
    # loader. Missing the env override lets an env-relocated identity look
    # "fresh" and re-copy a live token.
    $statePath = Resolve-FallowStatePath -ConfigPath $ConfigDst -FallowHome $FallowHome `
        -UserProfile $UserProfile -EnvOverride (Get-FallowInstallEnv 'FALLOW_STATE_PATH')
    switch (Get-FallowInstallDisposition -StatePath $statePath) {
        'site' {
            Write-Log "an enrolled Site identity already exists at $statePath; keeping it and skipping the join bundle (re-installing the program and task only)"
            $SiteJoin = $null
            $SiteResume = $true
            # If the config is gone or is not Site-configured, the daemon rejects
            # the stored profile ("site_join_bundle is not configured"). Rebuild a
            # token-free Site config from the persisted identity, still without
            # the new token bundle. Resolve the staged binary now so we fail
            # before side effects, exactly like the fresh path.
            $SiteNeedsConfig = (-not (Test-Path $ConfigDst)) -or
                (-not (Read-FallowConfigValue -ConfigPath $ConfigDst -Key 'site_join_bundle'))
            if ($SiteNeedsConfig) {
                $SiteLlama = Resolve-FallowStagedLlama -DeployDir $DeployDir
                if (-not $SiteLlama) {
                    Throw-Err "no staged llama-server.exe under $(Join-Path $DeployDir 'bin\windows'); run deploy\windows\fetch-llama.ps1 first"
                }
            }
        }
        'fresh' {
            # A revoked identity reads as 'fresh': the join bundle is staged, and
            # the dead identity plus its marker are removed with it, below. Note
            # the paths now, before any side effect.
            $marker = Get-FallowRevokedMarkerPath -StatePath $statePath
            if (Test-Path -LiteralPath $marker -PathType Leaf) {
                $SiteStatePath = $statePath
                $SiteRevokedMarker = $marker
            }
            # Fail loudly now if the Windows llama-server is not staged: a Site
            # config that keeps the example's Unix path would let agentctl doctor
            # fail and the agent never serve.
            $SiteLlama = Resolve-FallowStagedLlama -DeployDir $DeployDir
            if (-not $SiteLlama) {
                Throw-Err "no staged llama-server.exe under $(Join-Path $DeployDir 'bin\windows'); run deploy\windows\fetch-llama.ps1 first"
            }
        }
    }

    # Site Mode serves llama on loopback only. If the scheduled user's
    # environment forces a non-loopback bind_host, the Go loader overrides the
    # rendered 127.0.0.1 and Site validation makes the daemon exit. Fail before
    # side effects so the operator clears the override.
    $bindOverride = Get-FallowInstallEnv 'FALLOW_BIND_HOST'
    if ($bindOverride -and -not (Test-FallowLoopbackHost $bindOverride)) {
        Throw-Err "FALLOW_BIND_HOST=$bindOverride overrides the loopback Site bind; clear it (User and Machine env) before installing Site Mode"
    }

    # FALLOW_SITE_JOIN_BUNDLE overrides the rendered site_join_bundle in the Go
    # loader, so a stale or wrong path would make the daemon read (and delete)
    # the wrong bundle instead of the validated protected copy - stranding the
    # token or enrolling into the wrong Site. Reject any override that is not the
    # managed path before any side effect.
    $joinOverride = Get-FallowInstallEnv 'FALLOW_SITE_JOIN_BUNDLE'
    if ($joinOverride) {
        $managed = $SiteJoinDst
        $override = Expand-FallowHome -Path $joinOverride -UserProfile $UserProfile
        try { $managed = [System.IO.Path]::GetFullPath($managed); $override = [System.IO.Path]::GetFullPath($override) } catch { $override = $joinOverride }
        if ($override -ne $managed) {
            Throw-Err "FALLOW_SITE_JOIN_BUNDLE=$joinOverride overrides the managed Site join path ($SiteJoinDst); clear it (User and Machine env) before installing Site Mode"
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
#
# Every substituted value lands in element text or an attribute, and a profile
# directory or an account name may legitimately carry &, < or >. Escape each one
# so the rendered task stays well-formed XML that Task Scheduler will parse.
function ConvertTo-FallowXmlText { param([string]$Value) [System.Security.SecurityElement]::Escape($Value) }
$xml = Get-Content -Raw -Path $XmlTemplate
$xml = $xml.Replace('__USERID__',  (ConvertTo-FallowXmlText $UserId))
$xml = $xml.Replace('__PYTHONW__', (ConvertTo-FallowXmlText $ProgramPath))
if ($GoBinary) {
    $xml = $xml.Replace('-m fallow_agent run --config', 'run -config')
}
$xml = $xml.Replace('__CONFIG__',  (ConvertTo-FallowXmlText $ConfigDst))
$xml = $xml.Replace('__WORKDIR__', (ConvertTo-FallowXmlText $WorkDir))

if ($DryRun) { Write-Output $xml; return }

# -- Backend: pick the build the machine can actually use --------------------
# fetch-llama.ps1 stages the matching binary; here we only report it and, on the
# CPU fallback, cap llama-server threads so the box stays usable.
$backend = Get-FallowBackend
Write-Log "backend: $backend"
if ($backend -eq 'cpu') {
    $threads = Get-FallowCpuThreadLimit
    if ($PSCmdlet.ShouldProcess("$UserId environment $ThreadEnv", "set to $threads")) {
        if ($UserSid) {
            # Another account's User scope is its registry hive, which exists
            # only while it is signed in. Skipping the cap costs politeness on a
            # CPU desk, not correctness, so warn rather than fail the install.
            if (Set-FallowTargetEnv -Sid $UserSid -Name $ThreadEnv -Value "$threads") {
                Write-Log "CPU build: capped $ThreadEnv=$threads for $UserId  (untested - verify on target)"
            } else {
                Write-Log "WARNING: could not cap $ThreadEnv for $UserId (signed out, hive not loaded); set it in their session with [Environment]::SetEnvironmentVariable('$ThreadEnv','$threads','User')"
            }
        } else {
            [Environment]::SetEnvironmentVariable($ThreadEnv, "$threads", 'User')
            Write-Log "CPU build: capped $ThreadEnv=$threads for the pilot account"
        }
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
        Write-Log "installing Go agent binary -> $AgentBin  (exercised in CI on windows-latest - verify on target)"
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
        # A revoked identity is dead and the marker beside it keeps the daemon
        # down, so both go before the new join bundle is staged. Full clean is
        # uninstall.ps1 -Purge; this is the narrow one the reinstall needs.
        if ($SiteRevokedMarker) {
            Remove-Item -LiteralPath $SiteStatePath -Force -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath $SiteRevokedMarker -Force -ErrorAction SilentlyContinue
            Write-Log "replaced a revoked identity: removed $SiteStatePath and its revocation marker before staging the new join bundle"
        }
        New-Item -ItemType Directory -Force -Path $SiteStateDir | Out-Null
        Protect-FallowSitePath -Path $SiteStateDir -UserId $UserId -AlsoAllow $AdminAlsoAllow -Directory
        # A re-run before the target's first logon (the MDM retry) copies over the
        # join file the last run left behind, and that file grants the task user
        # alone: an admin context holds no FILE_WRITE_DATA on it, so Copy-Item
        # -Force fails. Delete it first - the Site directory does grant the admin
        # principals, and FILE_DELETE_CHILD there is what removes the child.
        Remove-Item -LiteralPath $SiteJoinDst -Force -ErrorAction SilentlyContinue
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
        Protect-FallowSitePath -Path $ConfigDst -UserId $UserId -AlsoAllow $AdminAlsoAllow
        Write-Log "installed protected Site Mode join file and token-free config (llama_server_binary=$SiteLlama)"
        $llamaPath = Read-FallowConfigValue -ConfigPath $ConfigDst -Key 'llama_server_binary'
        if (-not $llamaPath -or -not (Test-Path -LiteralPath $llamaPath -PathType Leaf)) {
            Throw-Err "rendered llama_server_binary '$llamaPath' does not point at a file; the agent cannot serve"
        }
    }
} elseif ($SiteResume) {
    if ($SiteNeedsConfig) {
        if ($PSCmdlet.ShouldProcess($ConfigDst, 'reconstruct token-free Site config from the persisted identity')) {
            New-Item -ItemType Directory -Force -Path $SiteStateDir | Out-Null
            Protect-FallowSitePath -Path $SiteStateDir -UserId $UserId -AlsoAllow $AdminAlsoAllow -Directory
            if (-not (Test-Path $ConfigDst) -and (Test-Path $ConfigSrc)) { Copy-Item $ConfigSrc $ConfigDst }
            # site_join_bundle references the standard path even though the token
            # copy was consumed at enrollment: the daemon resumes Site Mode from
            # the persisted profile, and this key is what tells it to.
            Write-FallowSiteConfig -ConfigPath $ConfigDst -JoinBundlePath $SiteJoinDst -LlamaServerBinary $SiteLlama
            Protect-FallowSitePath -Path $ConfigDst -UserId $UserId -AlsoAllow $AdminAlsoAllow
            Write-Log "reconstructed token-free Site config from the persisted identity (site_join_bundle=$SiteJoinDst)"
        }
    } else {
        Write-Log "keeping existing Site config $ConfigDst"
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
    # Registration is proven on windows-latest; that the task then STARTS at a
    # real logon is not - a runner has no logon to test it with.
    Write-Log 'registered  (exercised in CI on windows-latest - verify on target)'
}

# Start it now so the user does not have to log out/in for first run. In admin
# context there is nothing to start: the task runs with an InteractiveToken in
# the target's session, which this context is not in, and the at-logon trigger
# is exactly what covers the wait. First-run enrollment happens then, from the
# staged join copy - the installer never enrolls over the network.
if ($User) {
    Write-Log "$UserId's task starts at their next logon (sign them out and back in to start now); the agent enrolls from the staged join file on that first run"
} elseif ($PSCmdlet.ShouldProcess($TaskName, 'start now')) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Log "started. inspect: Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo"
}
if ($User) {
    Write-Log "uninstall: deploy\windows\uninstall.ps1 -User '$User'"
} else {
    Write-Log 'uninstall: deploy\windows\uninstall.ps1'
}
