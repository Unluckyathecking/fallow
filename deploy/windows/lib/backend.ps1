<#
.SYNOPSIS
    Backend detection helpers shared by fetch-llama.ps1 and install.ps1.

.DESCRIPTION
    Decides whether this machine takes the CUDA or the CPU llama.cpp build, and
    picks a conservative CPU thread cap for the fallback. Dot-source it: it only
    defines functions and touches nothing on load.

    HONESTY: authored in a sandbox with no Windows host. The nvidia-smi and WMI
    probes are marked (untested - verify on target).
#>

function Get-FallowNvidiaPresent {
    <#
    .SYNOPSIS
        True when an NVIDIA GPU is usable on this machine.
    #>
    # nvidia-smi is the definitive check when the driver is installed.
    $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if ($smi) {
        try {
            & $smi.Source -L *> $null
            if ($LASTEXITCODE -eq 0) { return $true }
        } catch {
            # Driver present but the query failed; fall through to WMI.
        }
    }
    # WMI catches a card whose driver is not on PATH.
    try {
        $gpus = Get-CimInstance -ClassName Win32_VideoController -ErrorAction Stop
        foreach ($g in $gpus) {
            if ($g.Name -and $g.Name -match 'NVIDIA') { return $true }
        }
    } catch {
        # No CIM access; treat as no GPU rather than guess.
    }
    return $false
}

function Get-FallowBackend {
    <#
    .SYNOPSIS
        Resolve the llama.cpp build to use: 'cuda' or 'cpu'.
    .DESCRIPTION
        'auto' detects an NVIDIA GPU and picks CUDA, else CPU. An explicit
        'cuda' or 'cpu' is honoured as given. Never assumes CUDA.
    #>
    param([ValidateSet('auto', 'cuda', 'cpu')][string]$Requested = 'auto')

    if ($Requested -ne 'auto') { return $Requested }
    if (Get-FallowNvidiaPresent) { return 'cuda' }
    return 'cpu'
}

function Get-FallowCpuThreadLimit {
    <#
    .SYNOPSIS
        Conservative CPU thread cap for the CPU build.
    .DESCRIPTION
        Half the logical processors, clamped to [1, 4], so a shared pilot
        machine stays responsive while the CPU build serves work.
    #>
    $logical = [int]$env:NUMBER_OF_PROCESSORS
    if ($logical -lt 1) { $logical = 1 }
    $half = [Math]::Floor($logical / 2)
    return [int][Math]::Max(1, [Math]::Min(4, $half))
}
