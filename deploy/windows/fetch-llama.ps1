<#
.SYNOPSIS
    Download a PINNED llama.cpp build for Windows x64 and unpack it into
    deploy\bin\windows\, picking the CUDA or CPU build to match the machine.

.DESCRIPTION
    Backend selection (-Backend auto, the default): if an NVIDIA GPU is present
    the CUDA build is fetched, otherwise the CPU build. Nothing here assumes
    CUDA. Pass -Backend cuda or -Backend cpu to override the probe.

    THE CLASSIC CUDA TRAP: the `win-cuda` archive does NOT contain the CUDA
    runtime DLLs. The matching `cudart-llama-bin-win-*` archive has to be
    unpacked into the SAME directory, or llama-server.exe dies at launch with a
    missing cudart64_*.dll / cublas64_*.dll error. On the CUDA path this script
    fetches BOTH and unpacks them side by side. The CPU build needs neither.

    VERIFICATION: llama.cpp publishes no per-asset SHA256SUMS, so this script
    checks every download against llama-manifest.psd1 before it unpacks anything.
    A download whose hash is missing from the manifest, or does not match, is
    refused. Pin the hashes once on a trusted machine with -UpdateManifest,
    review the diff, and commit the manifest.

    HONESTY: authored in a sandbox with no network access. The release tag and
    the exact asset names MUST be verified against
    https://github.com/ggml-org/llama.cpp/releases before first use. Every
    network-dependent step is marked (untested - verify on target).

.PARAMETER Backend
    'auto' (default) probes for an NVIDIA GPU and picks cuda or cpu. 'cuda' or
    'cpu' forces the build.

.PARAMETER UpdateManifest
    Record the sha256 of each fetched asset into llama-manifest.psd1 instead of
    verifying against it. Run once on a trusted staging machine, then commit.
#>
[CmdletBinding()]
param(
    [ValidateSet('auto', 'cuda', 'cpu')][string]$Backend = 'auto',
    [switch]$UpdateManifest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# -- Pinned release (single source of truth) ---------------------------------
# Bump these together to move to a new llama.cpp build. The cu12.4 tag in the
# CUDA asset names MUST be identical, and llama-manifest.psd1 Release must match.
$LlamaRelease   = 'b4589'                                        # (untested - verify tag)
$CudaTag        = 'cu12.4'                                       # (untested - verify sub-version)
$LlamaCudaAsset = "llama-$LlamaRelease-bin-win-cuda-$CudaTag-x64.zip"
$CudartAsset    = "cudart-llama-bin-win-$CudaTag-x64.zip"
$LlamaCpuAsset  = "llama-$LlamaRelease-bin-win-cpu-x64.zip"

$GitHubRepo = 'ggml-org/llama.cpp'
$BaseUrl    = "https://github.com/$GitHubRepo/releases/download/$LlamaRelease"

# -- Paths -------------------------------------------------------------------
$ScriptDir    = Split-Path -Parent $MyInvocation.MyCommand.Path
$DeployDir    = Split-Path -Parent $ScriptDir
$BinDir       = Join-Path $DeployDir 'bin\windows'
$ManifestPath = Join-Path $ScriptDir 'llama-manifest.psd1'
$ServerExe    = 'llama-server.exe'

. (Join-Path $ScriptDir 'lib\backend.ps1')

function Write-Log { param([string]$Message) Write-Host "[fetch-llama] $Message" }

# -- Resolve backend and the asset set it needs ------------------------------
$resolved = Get-FallowBackend -Requested $Backend
Write-Log "backend: $resolved (requested $Backend)"

if ($resolved -eq 'cuda') {
    $assets = @(
        [pscustomobject]@{ Role = 'cuda';   Name = $LlamaCudaAsset },
        [pscustomobject]@{ Role = 'cudart'; Name = $CudartAsset }
    )
} else {
    $assets = @(
        [pscustomobject]@{ Role = 'cpu'; Name = $LlamaCpuAsset }
    )
}

# -- Load the manifest -------------------------------------------------------
if (-not (Test-Path $ManifestPath)) { throw "[fetch-llama] missing manifest $ManifestPath" }
$Manifest = Import-PowerShellDataFile -Path $ManifestPath
if ($Manifest.Release -ne $LlamaRelease) {
    throw "[fetch-llama] manifest Release=$($Manifest.Release) != pinned $LlamaRelease; refresh the manifest"
}

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
        throw "download failed for $AssetName - verify LlamaRelease=$LlamaRelease and the asset name on the releases page. $_"
    }
    return $archive
}

function Confirm-Hash {
    param([string]$Role, [string]$Archive)
    $hash = (Get-FileHash -Algorithm SHA256 -Path $Archive).Hash.ToLower()
    $want = [string]$Manifest.Sha256[$Role]
    if ([string]::IsNullOrEmpty($want)) {
        throw "manifest has no pinned sha256 for '$Role'; run -UpdateManifest on a trusted machine and commit llama-manifest.psd1"
    }
    if ($want -ne $hash) {
        throw "sha256 mismatch for '$Role': manifest $want != downloaded $hash. Refusing to unpack."
    }
    Write-Log "verified $Role sha256=$hash"
}

function Write-Manifest {
    param([hashtable]$Sha256)
    $lines = @(
        '<#'
        '    Pinned sha256 manifest for the Windows llama.cpp assets fetch-llama.ps1 pulls.'
        ''
        '    llama.cpp ships no per-asset SHA256SUMS, so this file is the trusted record.'
        '    Populate it once on a staging machine with `fetch-llama.ps1 -UpdateManifest`,'
        '    review the hashes in the diff, and commit. After that every install verifies'
        '    each download against the value here and refuses anything unknown or altered.'
        ''
        '    An empty string is a placeholder: the fetcher fails closed on it, so a stock'
        '    checkout will not run binaries until someone pins them. Keep Release in step'
        '    with $LlamaRelease in fetch-llama.ps1.'
        '#>'
        '@{'
        "    Release = '$LlamaRelease'"
        '    Sha256  = @{'
        "        cuda   = '$($Sha256.cuda)'"
        "        cudart = '$($Sha256.cudart)'"
        "        cpu    = '$($Sha256.cpu)'"
        '    }'
        '}'
    )
    Set-Content -Path $ManifestPath -Value $lines -Encoding UTF8
    Write-Log "wrote pinned hashes to $ManifestPath - review the diff and commit it"
}

try {
    # Download and verify (or record) every asset BEFORE anything is unpacked.
    $archives = @{}
    $recorded = @{ cuda = [string]$Manifest.Sha256.cuda; cudart = [string]$Manifest.Sha256.cudart; cpu = [string]$Manifest.Sha256.cpu }
    foreach ($a in $assets) {
        $archive = Get-Asset -AssetName $a.Name
        $archives[$a.Role] = $archive
        if ($UpdateManifest) {
            $recorded[$a.Role] = (Get-FileHash -Algorithm SHA256 -Path $archive).Hash.ToLower()
            Write-Log "recorded $($a.Role) sha256=$($recorded[$a.Role])"
        } else {
            Confirm-Hash -Role $a.Role -Archive $archive
        }
    }
    if ($UpdateManifest) { Write-Manifest -Sha256 $recorded }

    foreach ($a in $assets) {
        Write-Log "unpacking $($a.Role) into $BinDir"
        Expand-Archive -Path $archives[$a.Role] -DestinationPath $BinDir -Force
    }
    if ($resolved -eq 'cuda') {
        Write-Log 'cudart DLLs unpacked next to llama-server.exe (fixes the missing-DLL trap)'
    }

    $serverPath = Get-ChildItem -Path $BinDir -Recurse -Filter $ServerExe | Select-Object -First 1
    if (-not $serverPath) {
        throw "$ServerExe not found after unpack - inspect archive layout (untested - verify on target)"
    }
    Write-Log "installed llama-server -> $($serverPath.FullName)"
    Write-Log "point llama_server_binary at that path in %USERPROFILE%\.fallow\agent.toml"
    Write-Log 'done'
} finally {
    Remove-Item -Recurse -Force $TmpDir -ErrorAction SilentlyContinue
}
