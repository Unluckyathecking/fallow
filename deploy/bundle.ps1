<#
.SYNOPSIS
    Verify or install a Fallow offline bundle on Windows x64.
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('Verify', 'Install')]
    [string]$Command = 'Verify',
    [string]$Bundle = $PSScriptRoot,
    [string]$Prefix = (Join-Path $HOME '.fallow\offline'),
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Test-Bundle {
    param([string]$Path)
    $root = (Resolve-Path $Path).Path
    $manifest = Join-Path $root 'manifest.sha256'
    if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) {
        throw 'manifest.sha256 is missing'
    }
    $link = Get-ChildItem -LiteralPath $root -Recurse -Force | Where-Object {
        $_.Attributes -band [IO.FileAttributes]::ReparsePoint
    } | Select-Object -First 1
    if ($link) { throw 'bundle contains a symbolic link or reparse point' }
    $rootPrefix = $root.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    $seen = @{}
    $count = 0
    foreach ($line in Get-Content -LiteralPath $manifest) {
        if ($line -notmatch '^([0-9a-f]{64})  (.+)$') {
            throw 'invalid manifest line'
        }
        $expected = $Matches[1]
        $relative = $Matches[2]
        if ([IO.Path]::IsPathRooted($relative) -or $relative -eq 'manifest.sha256') {
            throw "unsafe manifest path: $relative"
        }
        $full = [IO.Path]::GetFullPath((Join-Path $root $relative))
        if (-not $full.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "unsafe manifest path: $relative"
        }
        if ($seen.ContainsKey($full)) { throw "duplicate manifest path: $relative" }
        $seen[$full] = $true
        if (-not (Test-Path -LiteralPath $full -PathType Leaf)) {
            throw "missing bundle file: $relative"
        }
        $actual = (Get-FileHash -LiteralPath $full -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $expected) { throw "hash mismatch: $relative" }
        $count++
    }
    if ($count -eq 0) { throw 'manifest is empty' }
    $actual = @(Get-ChildItem -LiteralPath $root -File -Recurse -Force | Where-Object {
        $_.FullName -ne $manifest
    }).Count
    if ($actual -ne $seen.Count) { throw 'manifest does not cover every bundle file' }
    Write-Host "bundle: verified $count files"
    return $root
}

function Install-Bundle {
    $root = Test-Bundle -Path $Bundle
    if ($DryRun) {
        Write-Host "Would create $Prefix and install locked wheels, llama.cpp, models, and config."
        return
    }
    $python = Get-Command py -ErrorAction SilentlyContinue
    if (-not $python) { throw 'Python launcher py.exe is required' }
    New-Item -ItemType Directory -Force -Path $Prefix | Out-Null
    & py -3.12 -m venv (Join-Path $Prefix 'venv')
    if ($LASTEXITCODE -ne 0) { throw 'virtual environment creation failed' }
    $venvPython = Join-Path $Prefix 'venv\Scripts\python.exe'
    & $venvPython -m pip install --no-index `
        --find-links (Join-Path $root 'wheels\workspace') `
        --find-links (Join-Path $root 'wheels\windows-x64') `
        fallow-agent fallow-bench fallow-coordinator fallow-cli
    if ($LASTEXITCODE -ne 0) { throw 'offline wheel installation failed' }
    Copy-Item -Recurse -Force (Join-Path $root 'llama\windows-x64-cuda') (Join-Path $Prefix 'llama')
    Copy-Item -Recurse -Force (Join-Path $root 'models') (Join-Path $Prefix 'models')
    $agentConfig = Join-Path $Prefix 'agent.toml'
    if (-not (Test-Path -LiteralPath $agentConfig)) {
        $template = Get-Content -Raw -LiteralPath (Join-Path $root 'config\agent.toml')
        $llamaPath = (Join-Path $Prefix 'llama\llama-server.exe').Replace('\', '/')
        $rendered = $template -replace '(?m)^llama_server_binary = .+$', `
            "llama_server_binary = `"$llamaPath`""
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($agentConfig, $rendered, $utf8NoBom)
    }
    Write-Host "bundle: installed to $Prefix"
}

if ($Command -eq 'Install') {
    Install-Bundle
} else {
    Test-Bundle -Path $Bundle | Out-Null
}
