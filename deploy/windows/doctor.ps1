<#
.SYNOPSIS
    Diagnose a Windows Site Mode agent and print one JSON report.

.DESCRIPTION
    doctor.ps1 combines `agentctl doctor` (config, identity, llama path and the
    static pinned-TLS check) with the Windows-native facts that only the host can
    answer: is the at-logon Scheduled Task registered and running, is a user
    actually logged in, are the config and join file locked down, and is llama
    bound to loopback only. It prints a single JSON object and exits non-zero when
    a required check fails.

    It is read-only. It never enrolls, claims work, edits config or opens a
    coordinator connection to send a token. The optional -Probe switch adds a
    live TCP/TLS reach test to the pinned coordinator so a blocked port, an
    intercepting proxy and a pin mismatch can be told apart; without it the report
    is built entirely from local state.

    JSON keys: task_registered, task_running, interactive_session, config_acl,
    loopback_bind, llama_binary, spki_tls, clock, identity, ok. Each is
    {ok, detail} except ok, the overall required-checks result.

    HONESTY: authored in a sandbox. The Task Scheduler, session, ACL and listener
    calls are exercised on the target; anything not run there is called out.

.PARAMETER Config
    Path to the agent TOML. Defaults to %USERPROFILE%\.fallow\agent.toml.

.PARAMETER AgentBin
    Path to agentctl.exe. Defaults to %USERPROFILE%\.fallow\bin\agentctl.exe.

.PARAMETER Probe
    Also run a live TCP/TLS reach test against the pinned coordinator URL.
#>
[CmdletBinding()]
param(
    [string]$Config,
    [string]$AgentBin,
    [switch]$Probe
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Reuse the installer's TOML value decoder and env-precedence helpers so doctor
# reads state_path/bind_host exactly as the agent does (literal and basic TOML
# strings, FALLOW_* overrides).
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $ScriptDir 'new-site-config.ps1')

$FallowHome = Join-Path $env:USERPROFILE '.fallow'
if (-not $Config)   { $Config   = Join-Path $FallowHome 'agent.toml' }
if (-not $AgentBin) { $AgentBin = Join-Path $FallowHome 'bin\agentctl.exe' }
$TaskName = 'FallowAgent'
$TaskPath = '\Fallow\'
$SiteJoin = Join-Path $FallowHome 'site\join.json'

function New-Check { param([bool]$Ok, [string]$Detail) return [ordered]@{ ok = $Ok; detail = $Detail } }

# -- agentctl doctor: config, identity, llama, pinned_tls, clock --------------
# Reuse the Go agent's own read-only checks rather than reimplement config
# parsing or pin validation in PowerShell.
function Get-AgentDoctor {
    if (-not (Test-Path -LiteralPath $AgentBin)) {
        return $null, "agentctl not found at $AgentBin"
    }
    if (-not (Test-Path -LiteralPath $Config)) {
        return $null, "config not found at $Config"
    }
    try {
        $raw = & $AgentBin doctor -config $Config 2>&1
    } catch {
        $raw = $_.Exception.Message
    }
    $text = ($raw | Out-String).Trim()
    try {
        return ($text | ConvertFrom-Json), $null
    } catch {
        return $null, "agentctl doctor did not return JSON: $text"
    }
}

# -- Scheduled Task -----------------------------------------------------------
function Test-TaskRegistered {
    $task = Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction SilentlyContinue
    if (-not $task) { return (New-Check $false "task $TaskPath$TaskName is not registered") }
    return (New-Check $true "registered")
}

function Test-TaskRunning {
    $task = Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -ErrorAction SilentlyContinue
    if (-not $task) { return (New-Check $false 'task not registered') }
    if ($task.State -eq 'Running') { return (New-Check $true 'running') }
    $info = $task | Get-ScheduledTaskInfo -ErrorAction SilentlyContinue
    $last = if ($info) { "last result 0x{0:X}" -f $info.LastTaskResult } else { 'no run info' }
    return (New-Check $false ("state=$($task.State); $last"))
}

# -- Interactive session ------------------------------------------------------
# The task runs InteractiveToken, so it only serves while a user is logged in.
# Distinguish "no logged-in user" from a present, active session.
function Test-InteractiveSession {
    try { $sessions = @(quser 2>$null) } catch { $sessions = @() }
    if ($sessions.Count -gt 1) {
        $active = $sessions | Select-Object -Skip 1 | Where-Object { $_ -match '\bActive\b' }
        if ($active) { return (New-Check $true 'an interactive user session is active') }
        return (New-Check $true 'a user is logged in (disconnected); the task serves on reconnect')
    }
    $explorer = Get-Process explorer -ErrorAction SilentlyContinue
    if ($explorer) { return (New-Check $true 'a desktop session is present') }
    return (New-Check $false 'no logged-in user; the at-logon task cannot run until someone signs in')
}

# -- Config / join ACL --------------------------------------------------------
# Restrictive means no broad principal (Everyone, Users, Authenticated Users)
# holds any grant on the token-adjacent files.
function Test-RestrictiveAcl {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    $lines = & icacls.exe $Path 2>$null
    $broad = @('Everyone', 'BUILTIN\Users', 'NT AUTHORITY\Authenticated Users', 'NT AUTHORITY\INTERACTIVE')
    foreach ($line in $lines) {
        foreach ($p in $broad) { if ($line -like "*${p}:*") { return $p } }
    }
    return ''
}

function Test-ConfigAcl {
    $targets = @($Config)
    if (Test-Path -LiteralPath $SiteJoin) { $targets += $SiteJoin }
    $bad = @()
    $checked = @()
    foreach ($t in $targets) {
        $r = Test-RestrictiveAcl -Path $t
        if ($null -eq $r) { continue }
        $checked += (Split-Path -Leaf $t)
        if ($r -ne '') { $bad += ("$(Split-Path -Leaf $t): $r has access") }
    }
    if ($checked.Count -eq 0) { return (New-Check $false 'no config or join file to check') }
    if ($bad.Count -gt 0) { return (New-Check $false ("broad ACL grant: " + ($bad -join '; '))) }
    return (New-Check $true ("restricted: " + ($checked -join ', ')))
}

# -- Loopback bind ------------------------------------------------------------
# The single hard safety rule: llama replicas must never listen off loopback.
# Read the configured bind_host and, live, scan the replica port range for any
# non-loopback listener.
function Get-ConfigValue {
    # Table-scoped read, matching the agent: root keys (bind_host, state_path)
    # use the default table, and [port_range] keys pass -Table 'port_range'.
    param([string]$Key, [string]$Table = '')
    return (Read-FallowConfigValue -ConfigPath $Config -Key $Key -Table $Table)
}

function Test-LoopbackBind {
    $bind = Get-ConfigValue 'bind_host'
    if (-not $bind) { return (New-Check $false 'bind_host is unset') }
    $loopback = ($bind -eq '127.0.0.1' -or $bind -eq '::1' -or $bind -like '127.*' -or $bind -eq 'localhost')
    if (-not $loopback) {
        return (New-Check $false "bind_host=$bind is not loopback; Site Mode must serve llama over 127.0.0.1 only")
    }
    # Live: no listener in the port range may be bound to a routable address.
    $start = 8100; $count = 16
    $s = Get-ConfigValue 'start' 'port_range'; if ($s -and ($s -as [int])) { $start = [int]$s }
    $c = Get-ConfigValue 'count' 'port_range'; if ($c -and ($c -as [int])) { $count = [int]$c }
    $exposed = @()
    try {
        $listeners = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
            Where-Object { $_.LocalPort -ge $start -and $_.LocalPort -lt ($start + $count) }
        foreach ($l in $listeners) {
            $addr = $l.LocalAddress
            if ($addr -ne '127.0.0.1' -and $addr -ne '::1') { $exposed += "$addr`:$($l.LocalPort)" }
        }
    } catch { }
    if ($exposed.Count -gt 0) {
        return (New-Check $false ("llama replica listening off loopback: " + ($exposed -join ', ')))
    }
    return (New-Check $true "bind_host=$bind; no replica port exposed off loopback")
}

# -- Live coordinator reach (optional) ----------------------------------------
# Tell blocked TCP, an intercepting proxy and a pin mismatch apart. The agent
# remains the authoritative pin checker.
#
# The pin set and coordinator URL come from the persisted token-free identity
# profile first: after enrollment the Go runtime deletes the one-use join file,
# so a join-only lookup would find nothing on a normally enrolled machine and
# wrongly report the probe as unreachable. Fall back to the pre-enrollment join
# file. Neither source carries the enrollment token.
function Get-SiteProbeProfile {
    # FALLOW_STATE_PATH wins over the TOML state_path (Go config precedence).
    $statePath = Get-FallowPersistedEnv 'FALLOW_STATE_PATH'
    if (-not $statePath) { $statePath = Get-ConfigValue 'state_path' }
    if ($statePath) {
        $statePath = Expand-FallowHome $statePath
    } else {
        $statePath = Join-Path $FallowHome 'agent-state.json'
    }
    if (Test-Path -LiteralPath $statePath) {
        try {
            $id = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
            if (($id.PSObject.Properties.Name -contains 'site') -and $id.site -and $id.site.coordinator_urls) {
                return [pscustomobject]@{
                    Urls   = @($id.site.coordinator_urls)
                    Pins   = @($id.site.coordinator_spki_sha256)
                    Source = 'persisted identity'
                }
            }
        } catch { }
    }
    if (Test-Path -LiteralPath $SiteJoin) {
        try {
            $j = Get-Content -LiteralPath $SiteJoin -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($j.coordinator_urls) {
                return [pscustomobject]@{
                    Urls   = @($j.coordinator_urls)
                    Pins   = @($j.coordinator_spki_sha256)
                    Source = 'join file'
                }
            }
        } catch { }
    }
    return $null
}

function Invoke-ReachProbe {
    $siteProfile = Get-SiteProbeProfile
    if (-not $siteProfile) { return (New-Check $false 'no coordinator URL in the persisted identity or join file to probe') }
    $u = [uri](@($siteProfile.Urls)[0])
    $port = if ($u.Port -gt 0) { $u.Port } else { 443 }
    $target = [pscustomobject]@{ Host = $u.Host; Port = $port; Url = $u.AbsoluteUri }

    $tcp = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $tcp.BeginConnect($target.Host, $target.Port, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne(5000)) {
            return (New-Check $false "blocked TCP: $($target.Host):$($target.Port) did not accept a connection in 5s")
        }
        $tcp.EndConnect($iar)
    } catch {
        return (New-Check $false "blocked TCP: cannot reach $($target.Host):$($target.Port) ($($_.Exception.Message))")
    }

    $leaf = $null
    $ssl = New-Object System.Net.Security.SslStream($tcp.GetStream(), $false,
        [System.Net.Security.RemoteCertificateValidationCallback]{ param($s,$c,$ch,$e) $script:__leaf = $c; return $true })
    try {
        $ssl.AuthenticateAsClient($target.Host)
        $leaf = $script:__leaf
    } catch {
        return (New-Check $false "TLS handshake failed to $($target.Host):$($target.Port) ($($_.Exception.Message)); a TLS-intercepting proxy or wrong port is likely")
    } finally {
        $ssl.Dispose(); $tcp.Close()
    }
    if (-not $leaf) { return (New-Check $false 'TLS completed but no certificate was presented') }

    $cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($leaf)
    $spki = $null
    try { $spki = $cert.PublicKey.ExportSubjectPublicKeyInfo() } catch { $spki = $null }
    if (-not $spki) {
        # PowerShell 5.1 / .NET Framework has no ExportSubjectPublicKeyInfo, and
        # the handshake callback above accepted any certificate, so this reach
        # proves nothing about the pin. Do NOT report success: an intercepting
        # proxy would otherwise pass. Preserve agentctl's authoritative pinned
        # TLS result and only annotate it with the reachability finding.
        return ([ordered]@{
            ok       = $false
            preserve = $true
            detail   = "reachable; TLS ok to $($target.Host):$($target.Port), but live SPKI pin comparison needs pwsh 7+/.NET 5+, so agentctl remains the pin authority"
        })
    }
    $sha = [System.Security.Cryptography.SHA256]::Create().ComputeHash($spki)
    $pin = 'sha256/' + [Convert]::ToBase64String($sha)
    $pins = @($siteProfile.Pins)
    if ($pins -ccontains $pin) {
        return (New-Check $true "reachable; presented cert SPKI matches a pinned key ($($siteProfile.Source))")
    }
    return (New-Check $false "pin mismatch: server SPKI $pin is not in the pin set ($($siteProfile.Source)); this is the signature of a TLS-intercepting proxy - do not proceed")
}

# -- Assemble the report ------------------------------------------------------
$agent, $agentErr = Get-AgentDoctor

$report = [ordered]@{}
$report.task_registered    = Test-TaskRegistered
$report.task_running       = Test-TaskRunning
$report.interactive_session = Test-InteractiveSession
$report.config_acl         = Test-ConfigAcl
$report.loopback_bind      = Test-LoopbackBind

if ($agent) {
    $report.llama_binary = New-Check ([bool]$agent.llama.ok) $agent.llama.detail
    $report.identity     = New-Check ([bool]$agent.identity.ok) $agent.identity.detail
    if (-not $agent.config.ok) {
        # A config that agentctl cannot even load makes the other agent lanes
        # meaningless; surface it on identity so the operator sees the reason.
        $report.identity = New-Check $false ("config invalid: " + $agent.config.detail)
    }
    $report.spki_tls = New-Check ([bool]$agent.pinned_tls.ok) $agent.pinned_tls.detail
    # Clock skew is measured by the agent over its pinned client. Render what it
    # reported; doctor.ps1 owns no clock logic. An older agentctl that predates
    # the check reports nothing, which is not a fault of this machine's clock.
    if ($agent.PSObject.Properties.Name -contains 'clock') {
        $report.clock = New-Check ([bool]$agent.clock.ok) $agent.clock.detail
    } else {
        $report.clock = New-Check $true 'this agentctl reports no clock check'
    }
} else {
    $report.llama_binary = New-Check $false $agentErr
    $report.identity     = New-Check $false $agentErr
    $report.spki_tls     = New-Check $false $agentErr
    $report.clock        = New-Check $false $agentErr
}

if ($Probe) {
    $reach = Invoke-ReachProbe
    if ($reach.Contains('preserve') -and $reach['preserve']) {
        # Keep agentctl's authoritative pinned_tls result; append the reach note
        # rather than overwrite a verified result with an unverifiable probe.
        $report.spki_tls.detail = ((@($report.spki_tls.detail, $reach['detail']) | Where-Object { $_ }) -join '; ')
    } else {
        $report.spki_tls = $reach
    }
}

# Required checks decide the exit code. interactive_session and task_running are
# reported but not required: doctor is legitimately run headless or pre-login.
$required = @('task_registered', 'config_acl', 'loopback_bind', 'llama_binary', 'spki_tls', 'clock', 'identity')
$ok = $true
foreach ($k in $required) { if (-not $report[$k].ok) { $ok = $false } }
$report.ok = $ok

$report | ConvertTo-Json -Depth 5
if (-not $ok) { exit 1 }
