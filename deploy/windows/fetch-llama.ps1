<#
.SYNOPSIS
    Download a PINNED llama.cpp CUDA release for Windows x64 and unpack it into
    deploy\bin\windows\.

.DESCRIPTION
    THE CLASSIC TRAP: the llama.cpp `win-cuda` archive does NOT contain the CUDA
    runtime DLLs. You MUST also download the matching `cudart-llama-bin-win-*`
    archive and unpack it into the SAME directory, or llama-server.exe fails at
    launch with a missing cudart64_*.dll / cublas64_*.dll error. This script
    fetches BOTH and unpacks them side by side.

    The CUDA sub-version of the two archives MUST match (e.g. both cu12.4). They
    are pinned together in the two variables below.

    llama.cpp publishes no per-asset SHA256SUMS file, so this script records the
    SHA256 of what it downloaded into deploy\llama-version.lock and verifies
    against it on subsequent runs.

    HONESTY: authored in a sandbox with no network access. The release tag and
    the exact asset names MUST be verified against
    https://github.com/ggml-org/llama.cpp/releases before first use. Every
    network-dependent step is marked (untested - verify on target).
#>
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# -- Pinned release (single source of truth) ---------------------------------
# Bump these together to move to a new llama.cpp build. The cu12.4 tag in the
# two asset names MUST be identical.
$LlamaRelease   = 'b4589'                                        # (untested - verify tag)
$CudaTag        = 'cu12.4'                                       # (untested - verify sub-version)
$LlamaCudaAsset = "llama-$LlamaRelease-bin-win-cuda-$CudaTag-x64.zip"
$CudartAsset    = "cudart-llama-bin-win-$CudaTag-x64.zip"

$GitHubRepo = 'ggml-org/llama.cpp'
$BaseUrl    = "https://github.com/$GitHubRepo/releases/download/$LlamaRelease"

# -- Paths -------------------------------------------------------------------
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$DeployDir  = Split-Path -Parent $ScriptDir
$BinDir     = Join-Path $DeployDir 'bin\windows'
$LockFile   = Join-Path $DeployDir 'llama-version.lock'
$ServerExe  = 'llama-server.exe'

function Write-Log { param([string]$Message) Write-Host "[fetch-llama] $Message" }

New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
$TmpDir = New-Item -ItemType Directory -Force -Path (Join-Path $env:TEMP ("fallow-llama-" + [guid]::NewGuid()))

function Get-Asset {
    param([string]$AssetName)
    $url     = "$BaseUrl/$AssetName"
    $archive = Join-Path $TmpDir $AssetName
    Write-Log "downloading $url  (untested - verify on target)"
    try {
        Invoke-WebRequest -Uri $url -OutFile $archive -UseBasicParsing
    } catch {
        throw "download failed for $AssetName - verify LlamaRelease=$LlamaRelease and asset name on the releases page. $_"
    }
    $hash = (Get-FileHash -Algorithm SHA256 -Path $archive).Hash.ToLower()
    Write-Log "downloaded $AssetName sha256=$hash"

    # Lockfile: record on first run, verify on subsequent runs.
    $lockKey = "$LlamaRelease/$AssetName"
    $existing = $null
    if (Test-Path $LockFile) {
        $existing = Get-Content $LockFile | Where-Object { $_ -like "$lockKey *" } | Select-Object -First 1
    }
    if ($existing) {
        $want = ($existing -split '\s+')[1]
        if ($want -ne $hash) {
            throw "hash mismatch for ${lockKey}: locked $want != downloaded $hash"
        }
        Write-Log "hash matches lockfile for $AssetName"
    } else {
        Add-Content -Path $LockFile -Value "$lockKey $hash"
        Write-Log "recorded $lockKey -> $hash in $LockFile"
    }
    return $archive
}

try {
    # Order matters only for readability; both unpack into the same BinDir so
    # the cudart DLLs sit next to llama-server.exe.
    $llamaArchive  = Get-Asset -AssetName $LlamaCudaAsset
    $cudartArchive = Get-Asset -AssetName $CudartAsset

    Write-Log "unpacking llama.cpp CUDA build into $BinDir"
    Expand-Archive -Path $llamaArchive -DestinationPath $BinDir -Force
    Write-Log "unpacking cudart DLLs into $BinDir (fixes the missing-DLL trap)"
    Expand-Archive -Path $cudartArchive -DestinationPath $BinDir -Force

    $serverPath = Get-ChildItem -Path $BinDir -Recurse -Filter $ServerExe |
                  Select-Object -First 1
    if (-not $serverPath) {
        throw "$ServerExe not found after unpack - inspect archive layout (untested - verify on target)"
    }
    Write-Log "installed llama-server -> $($serverPath.FullName)"
    Write-Log "point supervisor.llama_binary at that path in %USERPROFILE%\.fallow\agent.toml"
    Write-Log 'done'
} finally {
    Remove-Item -Recurse -Force $TmpDir -ErrorAction SilentlyContinue
}
