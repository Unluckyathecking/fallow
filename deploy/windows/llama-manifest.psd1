<#
    Pinned sha256 manifest for the Windows llama.cpp assets fetch-llama.ps1 pulls.

    llama.cpp ships no per-asset SHA256SUMS, so this file is the trusted record.
    Populate it once on a staging machine with `fetch-llama.ps1 -UpdateManifest`,
    review the hashes in the diff, and commit. After that every install verifies
    each download against the value here and refuses anything unknown or altered.

    An empty string is a placeholder: the fetcher fails closed on it, so a stock
    checkout will not run binaries until someone pins them. Keep Release in step
    with $LlamaRelease in fetch-llama.ps1.
#>
@{
    Release = 'b4589'
    Sha256  = @{
        cuda   = ''
        cudart = ''
        cpu    = ''
    }
}
