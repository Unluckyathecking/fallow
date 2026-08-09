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
    ($obj | ConvertTo-Json -Depth 5) | Set-Content -LiteralPath $path -Encoding UTF8
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

Describe 'Protect-FallowSitePath ACL command' {
    It 'breaks inheritance and grants the user full control on a file' {
        $script:acl = @()
        Mock -CommandName 'icacls.exe' -MockWith { $script:acl = $args; $global:LASTEXITCODE = 0 }
        Protect-FallowSitePath -Path 'C:\x\join.json' -UserId 'DOMAIN\user'
        ($script:acl -contains '/inheritance:r') | Should Be $true
        ($script:acl -join ' ') | Should Match 'DOMAIN\\user:\(F\)'
    }
    It 'grants inheritable full control on a directory' {
        $script:acl = @()
        Mock -CommandName 'icacls.exe' -MockWith { $script:acl = $args; $global:LASTEXITCODE = 0 }
        Protect-FallowSitePath -Path 'C:\x\site' -UserId 'DOMAIN\user' -Directory
        ($script:acl -join ' ') | Should Match 'DOMAIN\\user:\(OI\)\(CI\)F'
    }
    It 'throws when icacls fails' {
        Mock -CommandName 'icacls.exe' -MockWith { $global:LASTEXITCODE = 1 }
        { Protect-FallowSitePath -Path 'C:\x\join.json' -UserId 'DOMAIN\user' } | Should Throw
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
