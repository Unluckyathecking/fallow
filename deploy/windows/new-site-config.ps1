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

# -- Raw JSON duplicate-key rejection -----------------------------------------
# Windows PowerShell 5.1 ships no streaming JSON tokenizer, and ConvertFrom-Json
# keeps only the last value for a repeated key. To match the Go client's
# recursive duplicate-key check we walk the raw token stream ourselves and throw
# on the first object that repeats a property name, at any depth. The input has
# already passed ConvertFrom-Json, so it is well-formed JSON; the walker only
# needs to track string boundaries and object scope, not re-validate the grammar.
function Assert-FallowJoinKeysUnique {
    param([Parameter(Mandatory)][string]$Text)
    $script:__fjText = $Text
    $script:__fjLen  = $Text.Length
    $script:__fjPos  = 0
    Read-FallowJsonValue
}

function Skip-FallowJsonSpace {
    while ($script:__fjPos -lt $script:__fjLen) {
        $c = $script:__fjText[$script:__fjPos]
        if ($c -eq ' ' -or $c -eq "`t" -or $c -eq "`n" -or $c -eq "`r") { $script:__fjPos++ } else { break }
    }
}

# Consume a JSON string starting at the current quote and return its decoded
# value. A single decoded scalar has no key ambiguity, so ConvertFrom-Json is a
# safe, correct unescaper (it handles \uXXXX and surrogate pairs) for the token.
function Read-FallowJsonString {
    $start = $script:__fjPos
    $script:__fjPos++  # opening quote
    while ($script:__fjPos -lt $script:__fjLen) {
        $c = $script:__fjText[$script:__fjPos]
        if ($c -eq '\') { $script:__fjPos += 2; continue }
        if ($c -eq '"') { $script:__fjPos++; break }
        $script:__fjPos++
    }
    $raw = $script:__fjText.Substring($start, $script:__fjPos - $start)
    return [string]($raw | ConvertFrom-Json)
}

function Read-FallowJsonValue {
    Skip-FallowJsonSpace
    if ($script:__fjPos -ge $script:__fjLen) { return }
    $c = $script:__fjText[$script:__fjPos]
    if ($c -eq '{') {
        $script:__fjPos++
        $seen = New-Object 'System.Collections.Generic.HashSet[string]'
        Skip-FallowJsonSpace
        if ($script:__fjPos -lt $script:__fjLen -and $script:__fjText[$script:__fjPos] -eq '}') { $script:__fjPos++; return }
        while ($script:__fjPos -lt $script:__fjLen) {
            Skip-FallowJsonSpace
            $key = Read-FallowJsonString
            if (-not $seen.Add($key)) { throw '[site] join file repeats a JSON key' }
            Skip-FallowJsonSpace
            if ($script:__fjPos -lt $script:__fjLen -and $script:__fjText[$script:__fjPos] -eq ':') { $script:__fjPos++ }
            Read-FallowJsonValue
            Skip-FallowJsonSpace
            if ($script:__fjPos -ge $script:__fjLen) { break }
            $d = $script:__fjText[$script:__fjPos]; $script:__fjPos++
            if ($d -eq '}') { break }
        }
    } elseif ($c -eq '[') {
        $script:__fjPos++
        Skip-FallowJsonSpace
        if ($script:__fjPos -lt $script:__fjLen -and $script:__fjText[$script:__fjPos] -eq ']') { $script:__fjPos++; return }
        while ($script:__fjPos -lt $script:__fjLen) {
            Read-FallowJsonValue
            Skip-FallowJsonSpace
            if ($script:__fjPos -ge $script:__fjLen) { break }
            $d = $script:__fjText[$script:__fjPos]; $script:__fjPos++
            if ($d -eq ']') { break }
        }
    } elseif ($c -eq '"') {
        [void](Read-FallowJsonString)
    } else {
        while ($script:__fjPos -lt $script:__fjLen) {
            $d = $script:__fjText[$script:__fjPos]
            if ($d -eq ',' -or $d -eq '}' -or $d -eq ']' -or
                $d -eq ' ' -or $d -eq "`t" -or $d -eq "`n" -or $d -eq "`r") { break }
            $script:__fjPos++
        }
    }
}

# -- Install preflight against a persisted identity ---------------------------
# The Go runtime returns immediately for an existing Site identity (it never
# consumes the freshly copied join file, so the token would sit live on disk)
# and refuses to convert an existing non-Site identity. Decide before any side
# effect: 'fresh' installs the bundle, 'site' keeps the enrolled agent and skips
# the bundle, and a non-Site identity is rejected outright.
function Get-FallowInstallDisposition {
    param([Parameter(Mandatory)][string]$StatePath)
    if (-not (Test-Path -LiteralPath $StatePath -PathType Leaf)) { return 'fresh' }
    try {
        $id = Get-Content -LiteralPath $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
    } catch {
        throw '[site] an identity file exists but is unreadable; refusing to overwrite it'
    }
    if (($id.PSObject.Properties.Name -contains 'site') -and $id.site) { return 'site' }
    throw '[site] an existing non-Site identity is present; the agent refuses to convert it to Site Mode. Remove it deliberately (uninstall.ps1 -Purge) before installing Site Mode.'
}

# Resolve the staged Windows llama-server.exe that fetch-llama.ps1 unpacks under
# deploy\bin\windows\ (possibly one directory deep). Returns the full path or
# $null; the caller fails loudly on $null so a Site install never ships the
# example's Unix llama_server_binary that agentctl doctor would reject.
function Resolve-FallowStagedLlama {
    param([Parameter(Mandatory)][string]$DeployDir)
    $binDir = Join-Path $DeployDir 'bin\windows'
    if (-not (Test-Path -LiteralPath $binDir)) { return $null }
    $exe = Get-ChildItem -Path $binDir -Recurse -Filter 'llama-server.exe' -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $exe) { return $null }
    return $exe.FullName
}

function Read-FallowSiteJoin {
    param([Parameter(Mandatory)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw '[site] join file does not exist'
    }
    $rawJoin = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
    try {
        $join = $rawJoin | ConvertFrom-Json -ErrorAction Stop
    } catch {
        throw '[site] join file is not valid JSON'
    }
    # ConvertFrom-Json is last-key-wins, so it silently accepts a bundle that
    # repeats a field such as enrollment_token; the Go client's strict parser
    # then rejects the same bytes only after install has copied the binary and
    # registered the task. Reject duplicates here, on the raw text, before any
    # side effect.
    Assert-FallowJoinKeysUnique -Text $rawJoin

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
        if (($pin -isnot [string]) -or ($pin -cnotmatch '^sha256/[A-Za-z0-9+/]{43}=$') -or
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

# Expand a leading ~ to the user profile so a persisted state_path resolves the
# way the Go agent's config loader does.
function Expand-FallowHome {
    param([Parameter(Mandatory)][string]$Path)
    if ($Path -eq '~' -or $Path.StartsWith('~/') -or $Path.StartsWith('~\')) {
        return (Join-Path $env:USERPROFILE $Path.Substring(1).TrimStart('/', '\'))
    }
    return $Path
}

# Read a single top-level scalar TOML value (unquoted) or $null when the file or
# key is absent. Used to resolve state_path before the identity preflight.
function Read-FallowConfigValue {
    param([Parameter(Mandatory)][string]$ConfigPath, [Parameter(Mandatory)][string]$Key)
    if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) { return $null }
    foreach ($line in Get-Content -LiteralPath $ConfigPath -Encoding UTF8) {
        if ($line -match ('^\s*' + [regex]::Escape($Key) + '\s*=\s*"?([^"#]+?)"?\s*(#.*)?$')) {
            return $Matches[1].Trim()
        }
    }
    return $null
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
        [Parameter(Mandatory)][string]$JoinBundlePath,
        [string]$LlamaServerBinary
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
        if ($LlamaServerBinary -and ($line -match '^\s*llama_server_binary\s*=')) { continue }
        $head.Add($line)
    }

    $siteKeys = @(
        "site_join_bundle = $(ConvertTo-FallowTomlString $JoinBundlePath)",
        'bind_host = "127.0.0.1"'
    )
    # On a fresh Site install the seeded example carries the Unix
    # llama_server_binary (/usr/local/bin/llama-server), which agentctl doctor
    # rejects on Windows. When the caller resolved the staged binary, own that
    # key too: strip the example value above and render the real path here.
    if ($LlamaServerBinary) {
        $siteKeys += "llama_server_binary = $(ConvertTo-FallowTomlString $LlamaServerBinary)"
    }
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
