<#
.SYNOPSIS
    Validates a Site Mode join file and renders its token-free local config.

.DESCRIPTION
    Called by install.ps1 before it makes any Site Mode change. It never writes an
    enrollment token into TOML: the protected join-file copy is the only temporary
    token holder, and the Go agent removes that token after it stores the enrolled
    identity and site profile (see docs/lan-site/join-bundle-v1.md).
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function ConvertTo-FallowTomlString {
    param([Parameter(Mandatory)][string]$Value)
    return '"' + $Value.Replace('\', '\\').Replace('"', '\"') + '"'
}

function Read-FallowSiteJoin {
    param([Parameter(Mandatory)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw '[site] join file does not exist'
    }
    try {
        $join = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
    } catch {
        throw '[site] join file is not valid JSON'
    }

    $required = @(
        'version', 'site_id', 'coordinator_urls', 'coordinator_spki_sha256', 'enrollment_token', 'mdns_service'
    )
    $names = @($join.PSObject.Properties.Name)
    if (($names.Count -ne $required.Count) -or ($names | Where-Object { $_ -notin $required })) {
        throw '[site] join file has unknown or missing fields'
    }
    if ((($join.version -isnot [int]) -and ($join.version -isnot [long])) -or ([long]$join.version -ne 1)) {
        throw '[site] join file version must be 1'
    }
    if (($join.site_id -isnot [string]) -or [string]::IsNullOrWhiteSpace($join.site_id) -or
        ($join.site_id.Length -gt 128)) {
        throw '[site] join file has an invalid site_id'
    }
    if (($join.enrollment_token -isnot [string]) -or [string]::IsNullOrWhiteSpace($join.enrollment_token)) {
        throw '[site] join file has no enrollment token'
    }
    if (($join.coordinator_urls -is [string]) -or @($join.coordinator_urls).Count -lt 1) {
        throw '[site] join file has no coordinator URLs'
    }
    $seenURLs = @{}
    foreach ($value in @($join.coordinator_urls)) {
        $uri = $null
        if (($value -isnot [string]) -or -not [uri]::TryCreate($value, [uriKind]::Absolute, [ref]$uri) -or
            ($uri.Scheme -ne 'https') -or [string]::IsNullOrEmpty($uri.Host) -or $uri.UserInfo -or
            $uri.Query -or $uri.Fragment -or (($uri.AbsolutePath -ne '') -and ($uri.AbsolutePath -ne '/')) -or
            $seenURLs.ContainsKey($value)) {
            throw '[site] join file has an invalid coordinator URL'
        }
        $seenURLs[$value] = $true
    }
    if (($join.coordinator_spki_sha256 -is [string]) -or @($join.coordinator_spki_sha256).Count -lt 1) {
        throw '[site] join file has no coordinator pins'
    }
    $seenPins = @{}
    foreach ($pin in @($join.coordinator_spki_sha256)) {
        if (($pin -isnot [string]) -or ($pin -notmatch '^sha256/[A-Za-z0-9+/]{43}=$') -or
            $seenPins.ContainsKey($pin)) {
            throw '[site] join file has an invalid coordinator pin'
        }
        $seenPins[$pin] = $true
    }
    if (($null -ne $join.mdns_service) -and (($join.mdns_service -isnot [string]) -or
        ($join.mdns_service -ne '_fallow._tcp.local.'))) {
        throw '[site] join file has an invalid mdns_service'
    }
    return $join
}

function Protect-FallowSitePath {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$UserId,
        [switch]$Directory
    )

    $inheritance = '/inheritance:r'
    $grant = "$UserId`:(F)"
    if ($Directory) { $grant = "$UserId`:(OI)(CI)F" }
    & icacls.exe $Path $inheritance '/grant:r' $grant | Out-Null
    if ($LASTEXITCODE -ne 0) { throw '[site] could not protect Site Mode state' }
}

function Write-FallowSiteConfig {
    param(
        [Parameter(Mandatory)][string]$ConfigPath,
        [Parameter(Mandatory)][string]$JoinBundlePath
    )

    $existing = ''
    if (Test-Path -LiteralPath $ConfigPath -PathType Leaf) {
        $existing = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8
    }
    # Site Mode owns coordinator selection and enrollment. Drop the legacy
    # top-level values rather than leave a cleartext URL or placeholder token
    # beside the join, and keep everything else the operator set (notably
    # llama_server_binary, which the daemon still requires).
    #
    # Split the file at the first TOML table header: managed keys are stripped
    # only from the top-level section, and the Site keys are inserted there too,
    # so a following [table] can never capture them.
    $head = New-Object System.Collections.Generic.List[string]
    $tail = New-Object System.Collections.Generic.List[string]
    $seenTable = $false
    foreach ($line in @($existing -split "`r?`n")) {
        if (-not $seenTable -and ($line -match '^\s*\[')) { $seenTable = $true }
        if ($seenTable) { $tail.Add($line); continue }
        if ($line -match '^\s*(coordinator_url|enrollment_token|bind_host|site_join_bundle)\s*=') { continue }
        $head.Add($line)
    }

    $siteKeys = @(
        "site_join_bundle = $(ConvertTo-FallowTomlString $JoinBundlePath)",
        'bind_host = "127.0.0.1"'
    )
    # Keep the interior of the top-level section (its comments and spacing) but
    # drop only the blank lines at its edges so the render stays deterministic.
    # Index math, not range slices: PowerShell's `1..0` counts backwards, so a
    # slice-based trim loops forever once the head reduces to a single blank line.
    $arr = @($head)
    $start = 0
    while ($start -lt $arr.Count -and [string]::IsNullOrWhiteSpace($arr[$start])) { $start++ }
    $stop = $arr.Count - 1
    while ($stop -ge $start -and [string]::IsNullOrWhiteSpace($arr[$stop])) { $stop-- }
    if ($stop -lt $start) { $headTrimmed = @() } else { $headTrimmed = @($arr[$start..$stop]) }

    $parts = @()
    if ($headTrimmed.Count -gt 0) { $parts += $headTrimmed }
    $parts += $siteKeys
    if ($tail.Count -gt 0) { $parts += @($tail) }
    $rendered = ($parts -join [Environment]::NewLine)

    $temporary = "$ConfigPath.new"
    Set-Content -LiteralPath $temporary -Value $rendered -Encoding UTF8
    Move-Item -LiteralPath $temporary -Destination $ConfigPath -Force
}
