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

# Mirror the Go client's decodePin: a pin is canonical only when it is
# "sha256/" + standard base64 that decodes to exactly 32 bytes and re-encodes to
# the same payload. Length-and-alphabet is not enough — a value like
# sha256/AAAA...AAB= passes the regex but carries noncanonical trailing bits the
# Go decoder rejects after install side effects, so validate it here.
function Test-FallowCanonicalPin {
    param([Parameter(Mandatory)][string]$Pin)
    if (-not $Pin.StartsWith('sha256/', [System.StringComparison]::Ordinal)) { return $false }
    $payload = $Pin.Substring(7)
    if ($payload -cnotmatch '^[A-Za-z0-9+/]{43}=$') { return $false }
    try { $bytes = [Convert]::FromBase64String($payload) } catch { return $false }
    if ($bytes.Length -ne 32) { return $false }
    return ([Convert]::ToBase64String($bytes) -ceq $payload)
}

function Read-FallowSiteJoin {
    param([Parameter(Mandatory)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw '[site] join file does not exist'
    }
    # Reject a UTF-8 BOM on the raw bytes: Get-Content -Encoding UTF8 strips it
    # before ConvertFrom-Json so validation would pass, but Copy-Item preserves
    # the original bytes and Go's encoding/json rejects a leading BOM only after
    # the installer has copied the token and registered the task. Fail here so
    # the copied bytes stay Go-parseable.
    $bomBytes = [System.IO.File]::ReadAllBytes($Path)
    if ($bomBytes.Length -ge 3 -and $bomBytes[0] -eq 0xEF -and $bomBytes[1] -eq 0xBB -and $bomBytes[2] -eq 0xBF) {
        throw '[site] join file has a UTF-8 byte-order mark; save it as BOM-free UTF-8'
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
    # Case-sensitive membership, matching Go's case-sensitive JSON key map: a
    # bundle using VERSION instead of version must fail preflight, not slip
    # through PowerShell's case-insensitive -notin and property access.
    $names = @($join.PSObject.Properties.Name)
    if (($names.Count -ne $required.Count) -or ($names | Where-Object { $_ -cnotin $required })) {
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
        if (($pin -isnot [string]) -or -not (Test-FallowCanonicalPin -Pin $pin) -or
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

# Decode one single-line TOML value the way the Go agent's BurntSushi/toml
# loader does: a literal string ('...') is verbatim, a basic string ("...")
# processes escapes, and a bare value runs to a comment or end of line. Returns
# $null on a malformed/empty value. Multi-line strings are out of scope for the
# single scalar keys this installer reads.
function ConvertFrom-FallowTomlValue {
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Raw)
    $s = $Raw.TrimStart()
    if ($s.Length -eq 0) { return $null }
    $q = $s[0]
    if ($q -eq "'") {
        $end = $s.IndexOf("'", 1)
        if ($end -lt 0) { return $null }
        return $s.Substring(1, $end - 1)
    }
    if ($q -eq '"') {
        $sb = New-Object System.Text.StringBuilder
        $i = 1
        while ($i -lt $s.Length) {
            $c = $s[$i]
            if ($c -eq '"') { return $sb.ToString() }
            if ($c -eq '\') {
                if ($i + 1 -ge $s.Length) { return $null }
                $n = $s[$i + 1]
                switch -CaseSensitive ($n) {
                    '"' { [void]$sb.Append('"'); $i += 2 }
                    '\' { [void]$sb.Append('\'); $i += 2 }
                    'b' { [void]$sb.Append([char]8);  $i += 2 }
                    't' { [void]$sb.Append([char]9);  $i += 2 }
                    'n' { [void]$sb.Append([char]10); $i += 2 }
                    'f' { [void]$sb.Append([char]12); $i += 2 }
                    'r' { [void]$sb.Append([char]13); $i += 2 }
                    'u' {
                        if ($i + 6 -gt $s.Length) { return $null }
                        $hex = $s.Substring($i + 2, 4)
                        if ($hex -cnotmatch '^[0-9A-Fa-f]{4}$') { return $null }
                        [void]$sb.Append([char][Convert]::ToInt32($hex, 16)); $i += 6
                    }
                    'U' {
                        if ($i + 10 -gt $s.Length) { return $null }
                        $hex = $s.Substring($i + 2, 8)
                        if ($hex -cnotmatch '^[0-9A-Fa-f]{8}$') { return $null }
                        [void]$sb.Append([char]::ConvertFromUtf32([Convert]::ToInt32($hex, 16))); $i += 10
                    }
                    default { return $null }
                }
            } else {
                [void]$sb.Append($c); $i++
            }
        }
        return $null  # unterminated basic string
    }
    $hash = $s.IndexOf('#')
    if ($hash -ge 0) { $s = $s.Substring(0, $hash) }
    $s = $s.Trim()
    if ($s.Length -eq 0) { return $null }
    return $s
}

# Read one scalar TOML value from a specific table (the root section by default),
# decoding every TOML string form exactly as the agent does. Table scope matters:
# BurntSushi/toml binds state_path/bind_host/etc to the root only, so a key that
# appears under a later [custom] table must not be attributed to the root.
function Read-FallowConfigValue {
    param(
        [Parameter(Mandatory)][string]$ConfigPath,
        [Parameter(Mandatory)][string]$Key,
        [string]$Table = ''
    )
    if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) { return $null }
    $current = ''
    foreach ($line in Get-Content -LiteralPath $ConfigPath -Encoding UTF8) {
        if ($line -match '^\s*\[+\s*([^\]]+?)\s*\]+\s*(#.*)?$') { $current = $Matches[1].Trim(); continue }
        if ($current -ne $Table) { continue }
        if ($line -match ('^\s*' + [regex]::Escape($Key) + '\s*=\s*(.*)$')) {
            return (ConvertFrom-FallowTomlValue -Raw $Matches[1])
        }
    }
    return $null
}

# Resolve an agent environment override the way the scheduled task's process
# will see it: a User variable wins over a Machine variable, with the installer's
# own process value as a last resort for a same-session run.
function Get-FallowPersistedEnv {
    param([Parameter(Mandatory)][string]$Name)
    foreach ($scope in 'User', 'Machine', 'Process') {
        $v = [Environment]::GetEnvironmentVariable($Name, $scope)
        if (-not [string]::IsNullOrEmpty($v)) { return $v }
    }
    return $null
}

# True when a bind_host value keeps llama on loopback only, mirroring the Go
# config loader's isLoopback exactly: a case-sensitive 'localhost'/'::1', else a
# parseable IP in the loopback range (127.0.0.0/8 or ::1).
function Test-FallowLoopbackHost {
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Value)
    if ($Value -ceq 'localhost' -or $Value -ceq '::1') { return $true }
    $addr = $null
    if ([System.Net.IPAddress]::TryParse($Value, [ref]$addr)) {
        return [System.Net.IPAddress]::IsLoopback($addr)
    }
    return $false
}

# Resolve the identity state path with the Go config loader's precedence:
# FALLOW_STATE_PATH wins over the TOML state_path, which wins over the default.
function Resolve-FallowStatePath {
    param(
        [Parameter(Mandatory)][string]$ConfigPath,
        [Parameter(Mandatory)][string]$FallowHome
    )
    $statePath = Get-FallowPersistedEnv 'FALLOW_STATE_PATH'
    if (-not $statePath) { $statePath = Read-FallowConfigValue -ConfigPath $ConfigPath -Key 'state_path' }
    if ($statePath) { return (Expand-FallowHome $statePath) }
    return (Join-Path $FallowHome 'agent-state.json')
}

function Protect-FallowSitePath {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$UserId,
        [switch]$Directory
    )

    # Rebuild the whole DACL to exactly one owner grant. icacls /inheritance:r
    # only drops INHERITED ACEs and /grant:r only replaces the named principal,
    # so a pre-existing explicit Everyone/Users grant on .fallow\site or
    # join.json would survive and could read the enrollment token. Protect the
    # descriptor (dropping inherited ACEs), remove every remaining explicit ACE,
    # then grant only the task user.
    $sec = Get-Acl -LiteralPath $Path
    $sec.SetAccessRuleProtection($true, $false)
    foreach ($rule in @($sec.Access)) { [void]$sec.RemoveAccessRule($rule) }
    $rights = [System.Security.AccessControl.FileSystemRights]::FullControl
    if ($Directory) {
        $inherit = [System.Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    } else {
        $inherit = [System.Security.AccessControl.InheritanceFlags]::None
    }
    $access = New-Object System.Security.AccessControl.FileSystemAccessRule(
        $UserId, $rights, $inherit,
        [System.Security.AccessControl.PropagationFlags]::None,
        [System.Security.AccessControl.AccessControlType]::Allow)
    $sec.AddAccessRule($access)
    try {
        Set-Acl -LiteralPath $Path -AclObject $sec
    } catch {
        throw '[site] could not protect Site Mode state'
    }
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
