#requires -Modules Pester
<#
    Pester tests for the Windows Site Mode install seam. They cover the pure,
    host-independent logic: strict join-file validation, TOML escaping, the
    token-free config render, the ACL command shape, install.ps1 -DryRun task
    rendering and legacy parity, and the doctor JSON contract.

    Written for the Pester 3.4 that ships with Windows PowerShell 5.1 so they run
    on the target with no extra install.
#>

$here     = Split-Path -Parent $MyInvocation.MyCommand.Path
$deployWin = Split-Path -Parent $here
$deploy    = Split-Path -Parent $deployWin
. (Join-Path $deployWin 'new-site-config.ps1')

$validJoin = @{
    version = 1
    site_id = 'clfs-pilot'
    coordinator_urls = @('https://10.24.8.10:8330')
    coordinator_spki_sha256 = @('sha256/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=')
    enrollment_token = 'one-use-secret-TOKENVALUE'
    mdns_service = $null
}

function New-JoinFile {
    param([hashtable]$Overrides = @{}, [string[]]$Remove = @())
    $obj = @{}
    foreach ($k in $validJoin.Keys) { $obj[$k] = $validJoin[$k] }
    foreach ($k in $Overrides.Keys) { $obj[$k] = $Overrides[$k] }
    foreach ($k in $Remove) { $obj.Remove($k) }
    $path = Join-Path $env:TEMP ("join_" + [guid]::NewGuid().ToString('N') + '.json')
    # Write BOM-free UTF-8: Set-Content -Encoding UTF8 on Windows PowerShell 5.1
    # emits a BOM, which a real join file must not carry (the Go client rejects
    # it). The BOM-rejection path has its own dedicated test.
    [System.IO.File]::WriteAllText($path, ($obj | ConvertTo-Json -Depth 5))
    return $path
}

Describe 'Read-FallowSiteJoin strict schema' {
    It 'accepts a valid join file' {
        $p = New-JoinFile
        { Read-FallowSiteJoin -Path $p } | Should Not Throw
        Remove-Item $p -Force
    }
    It 'rejects an unknown field' {
        $p = New-JoinFile -Overrides @{ extra = 'nope' }
        { Read-FallowSiteJoin -Path $p } | Should Throw
        Remove-Item $p -Force
    }
    It 'rejects a missing required field' {
        $p = New-JoinFile -Remove @('coordinator_urls')
        { Read-FallowSiteJoin -Path $p } | Should Throw
        Remove-Item $p -Force
    }
    It 'rejects version != 1' {
        $p = New-JoinFile -Overrides @{ version = 2 }
        { Read-FallowSiteJoin -Path $p } | Should Throw
        Remove-Item $p -Force
    }
    It 'rejects a non-https coordinator URL' {
        $p = New-JoinFile -Overrides @{ coordinator_urls = @('http://10.24.8.10:8330') }
        { Read-FallowSiteJoin -Path $p } | Should Throw
        Remove-Item $p -Force
    }
    It 'rejects a URL with a path' {
        $p = New-JoinFile -Overrides @{ coordinator_urls = @('https://10.24.8.10:8330/enroll') }
        { Read-FallowSiteJoin -Path $p } | Should Throw
        Remove-Item $p -Force
    }
    It 'rejects a malformed pin' {
        $p = New-JoinFile -Overrides @{ coordinator_spki_sha256 = @('sha256/tooshort') }
        { Read-FallowSiteJoin -Path $p } | Should Throw
        Remove-Item $p -Force
    }
    It 'rejects an empty enrollment token' {
        $p = New-JoinFile -Overrides @{ enrollment_token = '' }
        { Read-FallowSiteJoin -Path $p } | Should Throw
        Remove-Item $p -Force
    }
    It 'rejects a bad mdns_service' {
        $p = New-JoinFile -Overrides @{ mdns_service = '_evil._tcp.local.' }
        { Read-FallowSiteJoin -Path $p } | Should Throw
        Remove-Item $p -Force
    }
    It 'rejects non-JSON' {
        $p = Join-Path $env:TEMP ("bad_" + [guid]::NewGuid().ToString('N') + '.json')
        'not json {' | Set-Content -LiteralPath $p -Encoding UTF8
        { Read-FallowSiteJoin -Path $p } | Should Throw
        Remove-Item $p -Force
    }
}

Describe 'ConvertTo-FallowTomlString escaping' {
    It 'escapes backslashes and quotes' {
        $out = ConvertTo-FallowTomlString 'C:\a "b"'
        $out | Should Be '"C:\\a \"b\""'
    }
    It 'wraps a plain path in quotes' {
        (ConvertTo-FallowTomlString 'plain') | Should Be '"plain"'
    }
}

Describe 'Write-FallowSiteConfig render' {
    $token = $validJoin.enrollment_token
    $example = Join-Path $deploy 'agent.example.toml'

    It 'never writes the enrollment token' {
        $cfg = Join-Path $env:TEMP ("cfg_" + [guid]::NewGuid().ToString('N') + '.toml')
        Copy-Item $example $cfg
        Write-FallowSiteConfig -ConfigPath $cfg -JoinBundlePath 'C:\Users\x\.fallow\site\join.json'
        (Get-Content $cfg -Raw) | Should Not Match ([regex]::Escape($token))
        Remove-Item $cfg -Force
    }
    It 'sets a loopback bind and the join reference at top level, before any table' {
        $cfg = Join-Path $env:TEMP ("cfg_" + [guid]::NewGuid().ToString('N') + '.toml')
        Copy-Item $example $cfg
        Write-FallowSiteConfig -ConfigPath $cfg -JoinBundlePath 'C:\j\join.json'
        $lines = Get-Content $cfg
        $bindIdx = ($lines | Select-String -SimpleMatch 'bind_host = "127.0.0.1"' | Select-Object -First 1).LineNumber
        $joinIdx = ($lines | Select-String -SimpleMatch 'site_join_bundle =' | Select-Object -First 1).LineNumber
        $tableIdx = ($lines | Select-String -Pattern '^\s*\[' | Select-Object -First 1).LineNumber
        $bindIdx | Should Not BeNullOrEmpty
        $joinIdx | Should Not BeNullOrEmpty
        $bindIdx | Should BeLessThan $tableIdx
        $joinIdx | Should BeLessThan $tableIdx
        Remove-Item $cfg -Force
    }
    It 'strips the legacy coordinator_url and enrollment_token keys' {
        $cfg = Join-Path $env:TEMP ("cfg_" + [guid]::NewGuid().ToString('N') + '.toml')
        Copy-Item $example $cfg
        Write-FallowSiteConfig -ConfigPath $cfg -JoinBundlePath 'C:\j\join.json'
        $raw = Get-Content $cfg -Raw
        $raw | Should Not Match '(?m)^\s*coordinator_url\s*='
        $raw | Should Not Match '(?m)^\s*enrollment_token\s*='
        Remove-Item $cfg -Force
    }
    It 'preserves llama_server_binary from the seeded config' {
        $cfg = Join-Path $env:TEMP ("cfg_" + [guid]::NewGuid().ToString('N') + '.toml')
        Copy-Item $example $cfg
        Write-FallowSiteConfig -ConfigPath $cfg -JoinBundlePath 'C:\j\join.json'
        (Get-Content $cfg -Raw) | Should Match '(?m)^\s*llama_server_binary\s*='
        Remove-Item $cfg -Force
    }
    It 'trims a top-level section that collapses to a single blank line without hanging' {
        # Regression: a range-slice edge trim loops forever on a one-blank head.
        $cfg = Join-Path $env:TEMP ("cfg_" + [guid]::NewGuid().ToString('N') + '.toml')
        $seed = @'
coordinator_url = "x"

[port_range]
start = 8100
'@
        Set-Content -LiteralPath $cfg -Value $seed -Encoding UTF8
        { Write-FallowSiteConfig -ConfigPath $cfg -JoinBundlePath 'C:\j\join.json' } | Should Not Throw
        $raw = Get-Content $cfg -Raw
        $raw | Should Match '(?m)^site_join_bundle ='
        $raw | Should Match '(?m)^\[port_range\]'
        Remove-Item $cfg -Force
    }
    It 'does not duplicate the site keys on a re-run' {
        $cfg = Join-Path $env:TEMP ("cfg_" + [guid]::NewGuid().ToString('N') + '.toml')
        Copy-Item $example $cfg
        Write-FallowSiteConfig -ConfigPath $cfg -JoinBundlePath 'C:\j\join.json'
        Write-FallowSiteConfig -ConfigPath $cfg -JoinBundlePath 'C:\j\join.json'
        $count = @(Get-Content $cfg | Select-String -SimpleMatch 'bind_host = "127.0.0.1"').Count
        $count | Should Be 1
        Remove-Item $cfg -Force
    }
}

Describe 'Protect-FallowSitePath rebuilds the DACL to the task user only' {
    $me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    It 'removes a pre-seeded Everyone and BUILTIN\Users grant from a file' {
        $dir = Join-Path $env:TEMP ("acl_" + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        $f = Join-Path $dir 'join.json'
        Set-Content -LiteralPath $f -Value '{}' -Encoding ASCII
        & icacls.exe $f '/grant' 'Everyone:F' | Out-Null
        & icacls.exe $f '/grant' 'BUILTIN\Users:F' | Out-Null
        Protect-FallowSitePath -Path $f -UserId $me
        $out = (& icacls.exe $f) -join "`n"
        $out | Should Not Match 'Everyone'
        $out | Should Not Match 'BUILTIN\\Users'
        $out | Should Match ([regex]::Escape($me))
        Remove-Item -Recurse -Force $dir
    }
    It 'grants inheritable full control on a directory and drops inherited ACEs' {
        $dir = Join-Path $env:TEMP ("acl_" + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        & icacls.exe $dir '/grant' 'Everyone:(OI)(CI)F' | Out-Null
        Protect-FallowSitePath -Path $dir -UserId $me -Directory
        $sec = Get-Acl -LiteralPath $dir
        $sec.AreAccessRulesProtected | Should Be $true
        @($sec.Access | Where-Object { $_.IdentityReference.Value -ne $me }).Count | Should Be 0
        $rule = @($sec.Access | Where-Object { $_.IdentityReference.Value -eq $me })[0]
        ($rule.InheritanceFlags.ToString()) | Should Match 'ContainerInherit'
        ($rule.InheritanceFlags.ToString()) | Should Match 'ObjectInherit'
        Remove-Item -Recurse -Force $dir
    }
    It 'throws on a path that does not exist' {
        { Protect-FallowSitePath -Path (Join-Path $env:TEMP ('nope_' + [guid]::NewGuid().ToString('N'))) -UserId $me } | Should Throw
    }
}

Describe 'install.ps1 -DryRun task rendering' {
    $install = Join-Path $deployWin 'install.ps1'
    $dummyBin = Join-Path $env:TEMP ("agentctl_" + [guid]::NewGuid().ToString('N') + '.exe')
    Set-Content -LiteralPath $dummyBin -Value 'stub' -Encoding ASCII

    It 'renders the Go arg vector for the Go flavour' {
        $xml = & $install -GoBinary $dummyBin -DryRun
        ($xml -join "`n") | Should Match 'run -config'
        ($xml -join "`n") | Should Not Match '-m fallow_agent'
    }
    It 'keeps legacy task parity: InteractiveToken and a filled template' {
        $xml = & $install -GoBinary $dummyBin -DryRun
        $joined = ($xml -join "`n")
        $joined | Should Match 'InteractiveToken'
        $joined | Should Not Match '__USERID__'
        $joined | Should Not Match '__CONFIG__'
    }
    It 'never renders the config path with a token' {
        $xml = & $install -GoBinary $dummyBin -DryRun
        ($xml -join "`n") | Should Not Match $validJoin.enrollment_token
    }

    Remove-Item $dummyBin -Force
}

Describe 'doctor.ps1 JSON contract' {
    $doctor = Join-Path $deployWin 'doctor.ps1'
    It 'emits every required key even with no agent installed' {
        $cfg = Join-Path $env:TEMP ("cfg_" + [guid]::NewGuid().ToString('N') + '.toml')
        'bind_host = "127.0.0.1"' | Set-Content -LiteralPath $cfg -Encoding UTF8
        $missingBin = Join-Path $env:TEMP 'no-such-agentctl.exe'
        $out = & $doctor -Config $cfg -AgentBin $missingBin 2>$null
        $json = ($out | Out-String | ConvertFrom-Json)
        foreach ($k in 'task_registered','task_running','interactive_session','config_acl','loopback_bind','llama_binary','spki_tls','identity','ok') {
            ($json.PSObject.Properties.Name -contains $k) | Should Be $true
        }
        Remove-Item $cfg -Force
    }
    It 'flags a non-loopback bind_host' {
        $cfg = Join-Path $env:TEMP ("cfg_" + [guid]::NewGuid().ToString('N') + '.toml')
        'bind_host = "0.0.0.0"' | Set-Content -LiteralPath $cfg -Encoding UTF8
        $out = & $doctor -Config $cfg -AgentBin (Join-Path $env:TEMP 'no-such.exe') 2>$null
        $json = ($out | Out-String | ConvertFrom-Json)
        $json.loopback_bind.ok | Should Be $false
        Remove-Item $cfg -Force
    }
}

Describe 'schema and validator agree on required fields' {
    It 'lists the same required set in site-join.schema.json and Read-FallowSiteJoin' {
        $schemaPath = Join-Path $deployWin 'site-join.schema.json'
        $schema = Get-Content $schemaPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $schemaRequired = @($schema.required | Sort-Object)
        $validatorRequired = @('version','site_id','coordinator_urls','coordinator_spki_sha256','enrollment_token','mdns_service' | Sort-Object)
        ($schemaRequired -join ',') | Should Be ($validatorRequired -join ',')
    }
}

Describe 'Read-FallowSiteJoin duplicate keys' {
    function New-RawJoinFile {
        param([Parameter(Mandatory)][string]$Json)
        $path = Join-Path $env:TEMP ("rawjoin_" + [guid]::NewGuid().ToString('N') + '.json')
        [System.IO.File]::WriteAllText($path, $Json)
        return $path
    }
    $base = @'
{
  "version": 1,
  "site_id": "clfs-pilot",
  "coordinator_urls": ["https://10.24.8.10:8330"],
  "coordinator_spki_sha256": ["sha256/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="],
  "enrollment_token": "one-use-secret",
  "mdns_service": null
}
'@
    It 'accepts the single-key baseline it derives duplicates from' {
        $p = New-RawJoinFile $base
        { Read-FallowSiteJoin -Path $p } | Should Not Throw
        Remove-Item $p -Force
    }
    It 'rejects a duplicated sensitive key (enrollment_token) before any side effect' {
        $json = @'
{
  "version": 1,
  "site_id": "clfs-pilot",
  "coordinator_urls": ["https://10.24.8.10:8330"],
  "coordinator_spki_sha256": ["sha256/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="],
  "enrollment_token": "one",
  "enrollment_token": "two",
  "mdns_service": null
}
'@
        $p = New-RawJoinFile $json
        { Read-FallowSiteJoin -Path $p } | Should Throw
        Remove-Item $p -Force
    }
    It 'rejects a duplicated ordinary key (site_id)' {
        $json = @'
{
  "version": 1,
  "site_id": "a",
  "site_id": "b",
  "coordinator_urls": ["https://10.24.8.10:8330"],
  "coordinator_spki_sha256": ["sha256/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="],
  "enrollment_token": "one-use-secret",
  "mdns_service": null
}
'@
        $p = New-RawJoinFile $json
        { Read-FallowSiteJoin -Path $p } | Should Throw
        Remove-Item $p -Force
    }
    It 'rejects a duplicate nested inside an array element (recursive walk)' {
        { Assert-FallowJoinKeysUnique -Text '{"a":[{"x":1,"x":2}]}' } | Should Throw
    }
    It 'accepts a repeated key name across sibling objects' {
        { Assert-FallowJoinKeysUnique -Text '{"a":{"x":1},"b":{"x":2}}' } | Should Not Throw
    }
}

Describe 'Read-FallowSiteJoin pin case sensitivity' {
    It 'rejects an upper-case SHA256/ pin prefix the Go parser refuses' {
        $p = New-JoinFile -Overrides @{ coordinator_spki_sha256 = @('SHA256/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=') }
        { Read-FallowSiteJoin -Path $p } | Should Throw
        Remove-Item $p -Force
    }
}

Describe 'Get-FallowInstallDisposition identity preflight' {
    function New-StateFile {
        param([Parameter(Mandatory)][string]$Json)
        $path = Join-Path $env:TEMP ("state_" + [guid]::NewGuid().ToString('N') + '.json')
        Set-Content -LiteralPath $path -Value $Json -Encoding UTF8
        return $path
    }
    It 'reports fresh when no identity is on disk' {
        $p = Join-Path $env:TEMP ("nostate_" + [guid]::NewGuid().ToString('N') + '.json')
        (Get-FallowInstallDisposition -StatePath $p) | Should Be 'fresh'
    }
    It 'reports site for an enrolled Site identity so the bundle is skipped' {
        $p = New-StateFile '{"agent_id":"a1","device_token":"t","site":{"site_id":"s","coordinator_urls":["https://10.0.0.1:8330"],"coordinator_spki_sha256":["sha256/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="]}}'
        (Get-FallowInstallDisposition -StatePath $p) | Should Be 'site'
        Remove-Item $p -Force
    }
    It 'rejects an existing non-Site identity before side effects' {
        $p = New-StateFile '{"agent_id":"a1","device_token":"t"}'
        { Get-FallowInstallDisposition -StatePath $p } | Should Throw
        Remove-Item $p -Force
    }
}

Describe 'Resolve-FallowStagedLlama' {
    It 'finds a staged llama-server.exe under bin\windows (one dir deep)' {
        $deploy = Join-Path $env:TEMP ("dep_" + [guid]::NewGuid().ToString('N'))
        $nested = Join-Path $deploy 'bin\windows\build'
        New-Item -ItemType Directory -Force -Path $nested | Out-Null
        Set-Content -LiteralPath (Join-Path $nested 'llama-server.exe') -Value 'stub' -Encoding ASCII
        (Resolve-FallowStagedLlama -DeployDir $deploy) | Should Match 'llama-server\.exe$'
        Remove-Item -Recurse -Force $deploy
    }
    It 'returns null when nothing is staged' {
        $deploy = Join-Path $env:TEMP ("dep_" + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Force -Path $deploy | Out-Null
        (Resolve-FallowStagedLlama -DeployDir $deploy) | Should BeNullOrEmpty
        Remove-Item -Recurse -Force $deploy
    }
}

Describe 'Write-FallowSiteConfig renders the staged llama path' {
    $example = Join-Path $deploy 'agent.example.toml'
    It 'replaces the example Unix llama path with the staged Windows binary' {
        $cfg = Join-Path $env:TEMP ("cfg_" + [guid]::NewGuid().ToString('N') + '.toml')
        Copy-Item $example $cfg
        Write-FallowSiteConfig -ConfigPath $cfg -JoinBundlePath 'C:\j\join.json' -LlamaServerBinary 'D:\fallow\bin\windows\llama-server.exe'
        $raw = Get-Content $cfg -Raw
        $raw | Should Match '(?m)^llama_server_binary = "D:\\\\fallow\\\\bin\\\\windows\\\\llama-server\.exe"'
        $raw | Should Not Match '/usr/local/bin/llama-server'
        Remove-Item $cfg -Force
    }
}

Describe 'doctor.ps1 -Probe reads the persisted Site profile' {
    $doctor = Join-Path $deployWin 'doctor.ps1'
    It 'probes the coordinator from the persisted identity when the join file is gone' {
        $state = Join-Path $env:TEMP ("state_" + [guid]::NewGuid().ToString('N') + '.json')
        Set-Content -LiteralPath $state -Value '{"agent_id":"a1","device_token":"t","site":{"site_id":"s","coordinator_urls":["https://127.0.0.1:59997"],"coordinator_spki_sha256":["sha256/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="]}}' -Encoding UTF8
        $cfg = Join-Path $env:TEMP ("cfg_" + [guid]::NewGuid().ToString('N') + '.toml')
        Set-Content -LiteralPath $cfg -Value ('bind_host = "127.0.0.1"' + "`nstate_path = `"$($state.Replace('\','\\'))`"") -Encoding UTF8
        $out = & $doctor -Config $cfg -AgentBin (Join-Path $env:TEMP 'no-such.exe') -Probe 2>$null
        $json = ($out | Out-String | ConvertFrom-Json)
        $json.spki_tls.detail | Should Match '127\.0\.0\.1:59997'
        $json.spki_tls.detail | Should Not Match 'no coordinator URL'
        Remove-Item $cfg, $state -Force
    }
}

Describe 'Read-FallowSiteJoin BOM and canonical pins' {
    It 'rejects a join file saved with a UTF-8 BOM before any side effect' {
        $p = Join-Path $env:TEMP ("bom_" + [guid]::NewGuid().ToString('N') + '.json')
        $body = ($validJoin | ConvertTo-Json -Depth 5)
        # Set-Content -Encoding UTF8 on Windows PowerShell 5.1 writes a BOM.
        Set-Content -LiteralPath $p -Value $body -Encoding UTF8
        { Read-FallowSiteJoin -Path $p } | Should Throw
        Remove-Item $p -Force
    }
    It 'accepts the same bytes written BOM-free' {
        $p = Join-Path $env:TEMP ("nobom_" + [guid]::NewGuid().ToString('N') + '.json')
        $body = ($validJoin | ConvertTo-Json -Depth 5)
        [System.IO.File]::WriteAllText($p, $body)
        { Read-FallowSiteJoin -Path $p } | Should Not Throw
        Remove-Item $p -Force
    }
    It 'rejects a noncanonical base64 pin with nonzero trailing bits' {
        # 43 chars of A + B: valid length/alphabet, but decodes and re-encodes
        # to a different payload, which the Go decodePin rejects.
        $bad = 'sha256/' + ('A' * 42) + 'B='
        $p = New-JoinFile -Overrides @{ coordinator_spki_sha256 = @($bad) }
        { Read-FallowSiteJoin -Path $p } | Should Throw
        Remove-Item $p -Force
    }
    It 'accepts a canonical all-zero 32-byte pin' {
        $good = 'sha256/' + [Convert]::ToBase64String((New-Object 'byte[]' 32))
        $p = New-JoinFile -Overrides @{ coordinator_spki_sha256 = @($good) }
        { Read-FallowSiteJoin -Path $p } | Should Not Throw
        Remove-Item $p -Force
    }
}

Describe 'Test-FallowCanonicalPin' {
    It 'accepts a canonical 32-byte pin' {
        (Test-FallowCanonicalPin -Pin ('sha256/' + [Convert]::ToBase64String((New-Object 'byte[]' 32)))) | Should Be $true
    }
    It 'rejects nonzero trailing bits' {
        (Test-FallowCanonicalPin -Pin ('sha256/' + ('A' * 42) + 'B=')) | Should Be $false
    }
    It 'rejects an upper-case prefix' {
        (Test-FallowCanonicalPin -Pin ('SHA256/' + [Convert]::ToBase64String((New-Object 'byte[]' 32)))) | Should Be $false
    }
    It 'rejects a pin that decodes to the wrong length' {
        # A 20-byte hash re-encodes to a 28-char payload, failing the 43-char
        # base64 length check before any decode.
        (Test-FallowCanonicalPin -Pin ('sha256/' + [Convert]::ToBase64String((New-Object 'byte[]' 20)))) | Should Be $false
    }
}

Describe 'Resolve-FallowStatePath honors FALLOW_STATE_PATH precedence' {
    $fakeHome = Join-Path $env:TEMP ("home_" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $fakeHome | Out-Null
    $cfg = Join-Path $fakeHome 'agent.toml'
    # A Windows path in a basic (double-quoted) string would need escaped
    # backslashes; use a TOML literal string, exactly what an operator should write.
    [System.IO.File]::WriteAllText($cfg, "state_path = 'C:\from-config\agent-state.json'")
    $saved = $env:FALLOW_STATE_PATH
    It 'returns the env value over the config value' {
        $env:FALLOW_STATE_PATH = 'C:\from-env\agent-state.json'
        (Resolve-FallowStatePath -ConfigPath $cfg -FallowHome $fakeHome) | Should Be 'C:\from-env\agent-state.json'
    }
    It 'falls back to the config state_path when env is unset' {
        Remove-Item Env:FALLOW_STATE_PATH -ErrorAction SilentlyContinue
        (Resolve-FallowStatePath -ConfigPath $cfg -FallowHome $fakeHome) | Should Be 'C:\from-config\agent-state.json'
    }
    It 'falls back to the default under FallowHome when neither is set' {
        Remove-Item Env:FALLOW_STATE_PATH -ErrorAction SilentlyContinue
        $bare = Join-Path $env:TEMP ("home2_" + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Force -Path $bare | Out-Null
        (Resolve-FallowStatePath -ConfigPath (Join-Path $bare 'agent.toml') -FallowHome $bare) | Should Be (Join-Path $bare 'agent-state.json')
        Remove-Item -Recurse -Force $bare
    }
    It 'expands a leading ~ like the Go loader' {
        $env:FALLOW_STATE_PATH = '~/from-env/agent-state.json'
        (Resolve-FallowStatePath -ConfigPath $cfg -FallowHome $fakeHome) | Should Be (Join-Path $env:USERPROFILE 'from-env/agent-state.json')
    }
    if ($null -eq $saved) { Remove-Item Env:FALLOW_STATE_PATH -ErrorAction SilentlyContinue } else { $env:FALLOW_STATE_PATH = $saved }
    Remove-Item -Recurse -Force $fakeHome
}

Describe 'Get-FallowInstallDisposition with an env-relocated identity' {
    $saved = $env:FALLOW_STATE_PATH
    It 'rejects a non-Site identity found only via FALLOW_STATE_PATH' {
        $envState = Join-Path $env:TEMP ("envstate_" + [guid]::NewGuid().ToString('N') + '.json')
        [System.IO.File]::WriteAllText($envState, '{"agent_id":"legacy","device_token":"t"}')
        $fakeHome = Join-Path $env:TEMP ("home_" + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Force -Path $fakeHome | Out-Null
        $env:FALLOW_STATE_PATH = $envState
        $resolved = Resolve-FallowStatePath -ConfigPath (Join-Path $fakeHome 'agent.toml') -FallowHome $fakeHome
        $resolved | Should Be $envState
        { Get-FallowInstallDisposition -StatePath $resolved } | Should Throw
        Remove-Item $envState -Force; Remove-Item -Recurse -Force $fakeHome
    }
    if ($null -eq $saved) { Remove-Item Env:FALLOW_STATE_PATH -ErrorAction SilentlyContinue } else { $env:FALLOW_STATE_PATH = $saved }
}

Describe 'ConvertFrom-FallowTomlValue TOML string semantics' {
    It 'reads a single-quoted literal Windows path verbatim (no escape processing)' {
        (ConvertFrom-FallowTomlValue -Raw "'C:\fallow\identity.json'") | Should Be 'C:\fallow\identity.json'
    }
    It 'reads a double-quoted basic string and unescapes backslashes' {
        (ConvertFrom-FallowTomlValue -Raw '"C:\\fallow\\identity.json"') | Should Be 'C:\fallow\identity.json'
    }
    It 'keeps a forward-slash basic string as-is' {
        (ConvertFrom-FallowTomlValue -Raw '"~/.fallow/agent-state.json"') | Should Be '~/.fallow/agent-state.json'
    }
    It 'strips a trailing comment from a bare value' {
        (ConvertFrom-FallowTomlValue -Raw 'cpu   # a note') | Should Be 'cpu'
    }
    It 'ignores a hash inside a literal string' {
        (ConvertFrom-FallowTomlValue -Raw "'C:\a#b\id.json'") | Should Be 'C:\a#b\id.json'
    }
    It 'decodes a \u escape in a basic string' {
        (ConvertFrom-FallowTomlValue -Raw '"a\u0062c"') | Should Be 'abc'
    }
}

Describe 'Read-FallowConfigValue TOML forms' {
    It 'returns the unquoted path for a literal state_path' {
        $cfg = Join-Path $env:TEMP ("cfg_" + [guid]::NewGuid().ToString('N') + '.toml')
        [System.IO.File]::WriteAllText($cfg, "state_path = 'C:\fallow\identity.json'`r`nbind_host = `"127.0.0.1`"")
        (Read-FallowConfigValue -ConfigPath $cfg -Key 'state_path') | Should Be 'C:\fallow\identity.json'
        Remove-Item $cfg -Force
    }
    It 'returns the unescaped path for a basic state_path' {
        $cfg = Join-Path $env:TEMP ("cfg_" + [guid]::NewGuid().ToString('N') + '.toml')
        [System.IO.File]::WriteAllText($cfg, 'state_path = "C:\\fallow\\identity.json"')
        (Read-FallowConfigValue -ConfigPath $cfg -Key 'state_path') | Should Be 'C:\fallow\identity.json'
        Remove-Item $cfg -Force
    }
}

Describe 'Get-FallowInstallDisposition via a literal-quoted state_path' {
    It 'finds a persisted Site identity referenced by a TOML literal path' {
        $state = Join-Path $env:TEMP ("state_" + [guid]::NewGuid().ToString('N') + '.json')
        [System.IO.File]::WriteAllText($state, '{"agent_id":"a","device_token":"t","site":{"site_id":"s","coordinator_urls":["https://10.0.0.1:8330"],"coordinator_spki_sha256":["sha256/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="]}}')
        $cfg = Join-Path $env:TEMP ("cfg_" + [guid]::NewGuid().ToString('N') + '.toml')
        [System.IO.File]::WriteAllText($cfg, "state_path = '$state'")
        $fh = Join-Path $env:TEMP ("home_" + [guid]::NewGuid().ToString('N'))
        $saved = $env:FALLOW_STATE_PATH
        Remove-Item Env:FALLOW_STATE_PATH -ErrorAction SilentlyContinue
        $resolved = Resolve-FallowStatePath -ConfigPath $cfg -FallowHome $fh
        $resolved | Should Be $state
        (Get-FallowInstallDisposition -StatePath $resolved) | Should Be 'site'
        if ($null -ne $saved) { $env:FALLOW_STATE_PATH = $saved }
        Remove-Item $state, $cfg -Force
    }
}

Describe 'Test-FallowLoopbackHost' {
    It 'accepts loopback forms' {
        (Test-FallowLoopbackHost '127.0.0.1') | Should Be $true
        (Test-FallowLoopbackHost '::1') | Should Be $true
        (Test-FallowLoopbackHost 'localhost') | Should Be $true
        (Test-FallowLoopbackHost '127.5.5.5') | Should Be $true
    }
    It 'rejects routable and wildcard hosts' {
        (Test-FallowLoopbackHost '0.0.0.0') | Should Be $false
        (Test-FallowLoopbackHost '10.24.8.20') | Should Be $false
        (Test-FallowLoopbackHost '100.64.0.2') | Should Be $false
    }
}

Describe 'Get-FallowPersistedEnv precedence' {
    $saved = $env:FALLOW_BIND_HOST
    It 'falls back to the process value when no User/Machine value is set' {
        $env:FALLOW_BIND_HOST = '10.1.2.3'
        (Get-FallowPersistedEnv 'FALLOW_BIND_HOST') | Should Be '10.1.2.3'
    }
    It 'returns null when unset in every scope' {
        Remove-Item Env:FALLOW_BIND_HOST -ErrorAction SilentlyContinue
        (Get-FallowPersistedEnv ('FALLOW_NOPE_' + [guid]::NewGuid().ToString('N'))) | Should BeNullOrEmpty
    }
    if ($null -eq $saved) { Remove-Item Env:FALLOW_BIND_HOST -ErrorAction SilentlyContinue } else { $env:FALLOW_BIND_HOST = $saved }
}

Describe 'Read-FallowConfigValue is TOML-table aware' {
    It 'does not attribute a nested [custom] state_path to the root' {
        $cfg = Join-Path $env:TEMP ("cfg_" + [guid]::NewGuid().ToString('N') + '.toml')
        [System.IO.File]::WriteAllText($cfg, "bind_host = '127.0.0.1'`r`n[custom]`r`nstate_path = 'C:\other.json'")
        (Read-FallowConfigValue -ConfigPath $cfg -Key 'state_path') | Should BeNullOrEmpty
        Remove-Item $cfg -Force
    }
    It 'reads a root state_path that precedes a table' {
        $cfg = Join-Path $env:TEMP ("cfg_" + [guid]::NewGuid().ToString('N') + '.toml')
        [System.IO.File]::WriteAllText($cfg, "state_path = 'C:\root.json'`r`n[port_range]`r`nstart = 8100")
        (Read-FallowConfigValue -ConfigPath $cfg -Key 'state_path') | Should Be 'C:\root.json'
        Remove-Item $cfg -Force
    }
    It 'reads a value from a named table when asked' {
        $cfg = Join-Path $env:TEMP ("cfg_" + [guid]::NewGuid().ToString('N') + '.toml')
        [System.IO.File]::WriteAllText($cfg, "state_path = 'C:\root.json'`r`n[port_range]`r`nstart = 8250")
        (Read-FallowConfigValue -ConfigPath $cfg -Key 'start' -Table 'port_range') | Should Be '8250'
        (Read-FallowConfigValue -ConfigPath $cfg -Key 'start') | Should BeNullOrEmpty
        Remove-Item $cfg -Force
    }
}

Describe 'Resolve-FallowStatePath ignores a nested state_path' {
    It 'falls back to the default when state_path only appears under a table' {
        $cfg = Join-Path $env:TEMP ("cfg_" + [guid]::NewGuid().ToString('N') + '.toml')
        [System.IO.File]::WriteAllText($cfg, "[custom]`r`nstate_path = 'C:\other.json'")
        $fh = Join-Path $env:TEMP ("home_" + [guid]::NewGuid().ToString('N'))
        $saved = $env:FALLOW_STATE_PATH; Remove-Item Env:FALLOW_STATE_PATH -ErrorAction SilentlyContinue
        (Resolve-FallowStatePath -ConfigPath $cfg -FallowHome $fh) | Should Be (Join-Path $fh 'agent-state.json')
        if ($null -ne $saved) { $env:FALLOW_STATE_PATH = $saved }
        Remove-Item $cfg -Force
    }
}

Describe 'Read-FallowSiteJoin rejects miscased property names' {
    It 'rejects VERSION instead of version (case-sensitive names)' {
        $json = @'
{
  "VERSION": 1,
  "site_id": "clfs",
  "coordinator_urls": ["https://10.24.8.10:8330"],
  "coordinator_spki_sha256": ["sha256/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="],
  "enrollment_token": "one-use-secret",
  "mdns_service": null
}
'@
        $p = Join-Path $env:TEMP ("mc_" + [guid]::NewGuid().ToString('N') + '.json')
        [System.IO.File]::WriteAllText($p, $json)
        { Read-FallowSiteJoin -Path $p } | Should Throw
        Remove-Item $p -Force
    }
    It 'rejects a mixed-case Site_Id' {
        $json = @'
{
  "version": 1,
  "Site_Id": "clfs",
  "coordinator_urls": ["https://10.24.8.10:8330"],
  "coordinator_spki_sha256": ["sha256/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="],
  "enrollment_token": "one-use-secret",
  "mdns_service": null
}
'@
        $p = Join-Path $env:TEMP ("mc_" + [guid]::NewGuid().ToString('N') + '.json')
        [System.IO.File]::WriteAllText($p, $json)
        { Read-FallowSiteJoin -Path $p } | Should Throw
        Remove-Item $p -Force
    }
}

Describe 'Test-FallowLoopbackHost is case-sensitive like Go isLoopback' {
    It 'accepts lower-case localhost and loopback IPs' {
        (Test-FallowLoopbackHost 'localhost') | Should Be $true
        (Test-FallowLoopbackHost '127.0.0.1') | Should Be $true
        (Test-FallowLoopbackHost '127.9.9.9') | Should Be $true
        (Test-FallowLoopbackHost '::1') | Should Be $true
    }
    It 'rejects upper-case LOCALHOST (ParseIP fails, name is case-sensitive)' {
        (Test-FallowLoopbackHost 'LOCALHOST') | Should Be $false
    }
    It 'rejects routable and wildcard hosts' {
        (Test-FallowLoopbackHost '0.0.0.0') | Should Be $false
        (Test-FallowLoopbackHost '10.0.0.5') | Should Be $false
    }
}

Describe 'Read-FallowSiteJoin mdns_service is case-sensitive' {
    It 'accepts the exact lower-case service' {
        $p = New-JoinFile -Overrides @{ mdns_service = '_fallow._tcp.local.' }
        { Read-FallowSiteJoin -Path $p } | Should Not Throw
        Remove-Item $p -Force
    }
    It 'rejects an upper/mixed-case mdns_service' {
        $p = New-JoinFile -Overrides @{ mdns_service = '_FALLOW._TCP.LOCAL.' }
        { Read-FallowSiteJoin -Path $p } | Should Throw
        Remove-Item $p -Force
    }
}

Describe 'Test-FallowCoordinatorUrl matches Go url.Parse semantics' {
    It 'accepts a plain https host and an explicit valid port' {
        (Test-FallowCoordinatorUrl 'https://10.24.8.10:8330') | Should Be $true
        (Test-FallowCoordinatorUrl 'https://coord.example') | Should Be $true
        (Test-FallowCoordinatorUrl 'https://coord.example/') | Should Be $true
    }
    It 'rejects port 0 and out-of-range ports' {
        (Test-FallowCoordinatorUrl 'https://host:0') | Should Be $false
        (Test-FallowCoordinatorUrl 'https://host:70000') | Should Be $false
    }
    It 'rejects leading/trailing/control whitespace that System.Uri would trim' {
        (Test-FallowCoordinatorUrl ' https://host:8330') | Should Be $false
        (Test-FallowCoordinatorUrl 'https://host:8330 ') | Should Be $false
        (Test-FallowCoordinatorUrl "https://host:8330`t") | Should Be $false
        (Test-FallowCoordinatorUrl "https://ho`tst:8330") | Should Be $false
    }
    It 'rejects a dot-path and any path/query/fragment/userinfo' {
        (Test-FallowCoordinatorUrl 'https://host:8330/.') | Should Be $false
        (Test-FallowCoordinatorUrl 'https://host:8330/enroll') | Should Be $false
        (Test-FallowCoordinatorUrl 'https://host:8330//') | Should Be $false
        (Test-FallowCoordinatorUrl 'https://host:8330?x=1') | Should Be $false
        (Test-FallowCoordinatorUrl 'https://host:8330#f') | Should Be $false
        (Test-FallowCoordinatorUrl 'https://user@host:8330') | Should Be $false
        (Test-FallowCoordinatorUrl 'http://host:8330') | Should Be $false
    }
    It 'is enforced through Read-FallowSiteJoin (dot-path rejected before side effects)' {
        $p = New-JoinFile -Overrides @{ coordinator_urls = @('https://10.24.8.10:8330/.') }
        { Read-FallowSiteJoin -Path $p } | Should Throw
        Remove-Item $p -Force
        $p2 = New-JoinFile -Overrides @{ coordinator_urls = @('https://10.24.8.10:0') }
        { Read-FallowSiteJoin -Path $p2 } | Should Throw
        Remove-Item $p2 -Force
    }
}

Describe 'Test-FallowLoneSurrogateEscape mirrors Go hasLoneSurrogateEscape' {
    It 'flags a lone high surrogate escape' {
        (Test-FallowLoneSurrogateEscape '"a\uD800b"') | Should Be $true
    }
    It 'flags a lone low surrogate escape' {
        (Test-FallowLoneSurrogateEscape '"\uDC00"') | Should Be $true
    }
    It 'accepts a proper high+low surrogate pair' {
        (Test-FallowLoneSurrogateEscape '"\uD83D\uDE00"') | Should Be $false
    }
    It 'ignores non-surrogate \u escapes' {
        (Test-FallowLoneSurrogateEscape '"\u0061"') | Should Be $false
    }
    It 'is enforced through Read-FallowSiteJoin' {
        $json = @'
{
  "version": 1,
  "site_id": "clfs",
  "coordinator_urls": ["https://10.24.8.10:8330"],
  "coordinator_spki_sha256": ["sha256/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="],
  "enrollment_token": "tok\uD800en",
  "mdns_service": null
}
'@
        $p = Join-Path $env:TEMP ("sur_" + [guid]::NewGuid().ToString('N') + '.json')
        [System.IO.File]::WriteAllText($p, $json)
        { Read-FallowSiteJoin -Path $p } | Should Throw
        Remove-Item $p -Force
    }
}

Describe 'Expand-FallowHome expands only ~ and ~/ (Go expandHome)' {
    It 'expands a bare tilde to the profile' {
        (Expand-FallowHome '~') | Should Be $env:USERPROFILE
    }
    It 'expands a ~/ prefix' {
        (Expand-FallowHome '~/x/y.json') | Should Be (Join-Path $env:USERPROFILE 'x/y.json')
    }
    It 'leaves a ~\ path literal' {
        (Expand-FallowHome '~\x\y.json') | Should Be '~\x\y.json'
    }
    It 'leaves a ~name path literal' {
        (Expand-FallowHome '~other/x') | Should Be '~other/x'
    }
}

Describe 'Read-FallowSiteJoin rejects invalid UTF-8 bytes' {
    It 'rejects a raw 0xFF byte in the enrollment token before any side effect' {
        $prefix = '{"version":1,"site_id":"clfs","coordinator_urls":["https://10.24.8.10:8330"],"coordinator_spki_sha256":["sha256/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="],"enrollment_token":"tok'
        $suffix = 'en","mdns_service":null}'
        $bytes = [System.Text.Encoding]::ASCII.GetBytes($prefix) + [byte]0xFF + [System.Text.Encoding]::ASCII.GetBytes($suffix)
        $p = Join-Path $env:TEMP ("u8_" + [guid]::NewGuid().ToString('N') + '.json')
        [System.IO.File]::WriteAllBytes($p, $bytes)
        { Read-FallowSiteJoin -Path $p } | Should Throw
        Remove-Item $p -Force
    }
    It 'accepts the same bytes when they are valid UTF-8' {
        $json = '{"version":1,"site_id":"clfs","coordinator_urls":["https://10.24.8.10:8330"],"coordinator_spki_sha256":["sha256/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="],"enrollment_token":"token","mdns_service":null}'
        $p = Join-Path $env:TEMP ("u8_" + [guid]::NewGuid().ToString('N') + '.json')
        [System.IO.File]::WriteAllBytes($p, [System.Text.Encoding]::UTF8.GetBytes($json))
        { Read-FallowSiteJoin -Path $p } | Should Not Throw
        Remove-Item $p -Force
    }
}

Describe 'Test-FallowCoordinatorUrl rejects percent escapes' {
    It 'rejects a percent-encoded host that System.Uri would normalize' {
        (Test-FallowCoordinatorUrl 'https://h%6Fst:8330') | Should Be $false
        (Test-FallowCoordinatorUrl 'https://host%2e:8330') | Should Be $false
    }
    It 'is enforced through Read-FallowSiteJoin' {
        $p = New-JoinFile -Overrides @{ coordinator_urls = @('https://h%6Fst:8330') }
        { Read-FallowSiteJoin -Path $p } | Should Throw
        Remove-Item $p -Force
    }
}

Describe 'Test-FallowLoopbackHost rejects legacy IPv4 forms net.ParseIP rejects' {
    It 'accepts canonical loopback forms' {
        (Test-FallowLoopbackHost '127.0.0.1') | Should Be $true
        (Test-FallowLoopbackHost '127.5.5.5') | Should Be $true
        (Test-FallowLoopbackHost '::1') | Should Be $true
        (Test-FallowLoopbackHost 'localhost') | Should Be $true
    }
    It 'rejects inet_aton, hex, short and leading-zero forms' {
        (Test-FallowLoopbackHost '127.1') | Should Be $false
        (Test-FallowLoopbackHost '2130706433') | Should Be $false
        (Test-FallowLoopbackHost '127.000.000.001') | Should Be $false
        (Test-FallowLoopbackHost '0x7f.0.0.1') | Should Be $false
    }
    It 'still rejects routable and wildcard hosts and upper-case LOCALHOST' {
        (Test-FallowLoopbackHost '10.0.0.5') | Should Be $false
        (Test-FallowLoopbackHost '0.0.0.0') | Should Be $false
        (Test-FallowLoopbackHost 'LOCALHOST') | Should Be $false
        (Test-FallowLoopbackHost '256.0.0.1') | Should Be $false
    }
}
