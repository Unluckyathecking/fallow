<#
.SYNOPSIS
    Resolve a nominated Windows account for an admin-context install.

.DESCRIPTION
    install.ps1 and uninstall.ps1 normally act on the account running them. With
    -User they act on another account instead, from an elevated admin or SYSTEM
    context (Intune, ConfigMgr, PDQ, a GPO startup script). Everything that path
    needs about the target that the installer's own session cannot answer lives
    here: its SID and canonical name, its profile directory, and its registry
    environment. Dot-source it: it only defines functions and touches nothing on
    load.

    The agent itself still runs as an at-logon Scheduled Task in the target's own
    interactive session (ADR 017/063). Nothing here changes that; it only moves
    the registration off the target's keyboard.

    HONESTY: authored in a sandbox with no Windows host. Every function reads or
    writes real Windows state, and every one of them now runs on windows-latest -
    the Pester suite in ci.yml covers them directly, and the admin-context lane in
    install-acceptance.yml drives them through a real -User install. They are
    marked (exercised in CI on windows-latest - verify on target): a runner is
    still not a managed desk with a domain account and a roaming profile.
#>

function Test-FallowElevated {
    <#
    .SYNOPSIS
        True when this process can act on another account's profile and tasks.
    .DESCRIPTION
        SYSTEM is a member of the local Administrators group, so the same check
        covers a GPO startup script and an elevated admin shell.
        (exercised in CI on windows-latest - verify on target)
    #>
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Resolve-FallowTargetUser {
    <#
    .SYNOPSIS
        Resolve an account name to its SID, canonical name and profile directory.
    .DESCRIPTION
        Returns @{ Name; Sid; ProfilePath }. Name is the canonical
        DOMAIN\user (or MACHINE\user) form the ACL and the task principal need,
        which is not necessarily what the caller typed.

        The profile comes from the ProfileList registry, not from a guessed
        C:\Users\<name>: a roaming, relocated or renamed profile lives wherever
        that key says. A missing key means the account has never signed in here,
        which is a refusal - creating a profile is out of scope.
        (exercised in CI on windows-latest - verify on target)
    #>
    param([Parameter(Mandatory)][string]$Name)

    try {
        $account = New-Object System.Security.Principal.NTAccount($Name)
        $sid = $account.Translate([System.Security.Principal.SecurityIdentifier])
    } catch {
        throw "[user] cannot resolve account '$Name'; pass an account that exists on this machine or in its domain (user, MACHINE\user or DOMAIN\user)"
    }
    $canonical = $sid.Translate([System.Security.Principal.NTAccount]).Value

    $profileKey = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\$($sid.Value)"
    if (-not (Test-Path -LiteralPath $profileKey)) {
        throw "[user] '$canonical' has no user profile on this machine, so there is nothing to install into. Have them sign in once and re-run; creating a profile is out of scope for this installer."
    }
    # Read the whole key and test for the value: under Set-StrictMode a property
    # that is not there is an error, not $null.
    $item = Get-ItemProperty -LiteralPath $profileKey -ErrorAction SilentlyContinue
    $raw = ''
    if ($item -and ($item.PSObject.Properties.Name -contains 'ProfileImagePath')) {
        $raw = [string]$item.ProfileImagePath
    }
    if ([string]::IsNullOrWhiteSpace($raw)) {
        throw "[user] '$canonical' has a profile registration with no ProfileImagePath; the profile is damaged"
    }
    # ProfileImagePath is REG_EXPAND_SZ (%SystemDrive%\Users\...) on some builds.
    $profilePath = [Environment]::ExpandEnvironmentVariables($raw)
    if (-not (Test-Path -LiteralPath $profilePath -PathType Container)) {
        throw "[user] '$canonical' is registered with profile $profilePath, but that directory does not exist; the profile is damaged or has been cleaned up"
    }

    return [pscustomobject]@{
        Name        = $canonical
        Sid         = $sid.Value
        ProfilePath = $profilePath
    }
}

function Test-FallowUserHiveLoaded {
    <#
    .SYNOPSIS
        True when the target's registry hive is mounted under HKEY_USERS.
    .DESCRIPTION
        Windows mounts a user's hive while they are signed in. Signed out, their
        NTUSER.DAT is a file on disk and their environment cannot be read or
        written from here. This installer never loads a hive by hand.
        (exercised in CI on windows-latest - verify on target)
    #>
    param([Parameter(Mandatory)][string]$Sid)
    return (Test-Path -LiteralPath "Registry::HKEY_USERS\$Sid")
}

function Get-FallowTargetEnv {
    <#
    .SYNOPSIS
        Read one User-scope environment variable from the target's hive.
    .DESCRIPTION
        The Environment key under HKEY_USERS\<sid> is the same store
        [Environment]::GetEnvironmentVariable(..., 'User') reads for the current
        account. Returns $null when the hive is not loaded or the value is unset.
        (exercised in CI on windows-latest - verify on target)
    #>
    param(
        [Parameter(Mandatory)][string]$Sid,
        [Parameter(Mandatory)][string]$Name
    )
    $key = "Registry::HKEY_USERS\$Sid\Environment"
    if (-not (Test-Path -LiteralPath $key)) { return $null }
    $item = Get-ItemProperty -LiteralPath $key -ErrorAction SilentlyContinue
    # Case-insensitive on purpose: Windows environment variable names are.
    if (-not $item -or ($item.PSObject.Properties.Name -notcontains $Name)) { return $null }
    $value = [string]$item.$Name
    if ([string]::IsNullOrEmpty($value)) { return $null }
    return $value
}

function Set-FallowTargetEnv {
    <#
    .SYNOPSIS
        Write (or, with an empty value, remove) one User-scope variable in the
        target's hive. Returns $false when the hive is not loaded.
    .DESCRIPTION
        Mirrors [Environment]::SetEnvironmentVariable(..., 'User'), including its
        null-removes-the-value behaviour, for an account that is not this one.
        (exercised in CI on windows-latest - verify on target)
    #>
    param(
        [Parameter(Mandatory)][string]$Sid,
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][AllowEmptyString()][AllowNull()][string]$Value
    )
    $key = "Registry::HKEY_USERS\$Sid\Environment"
    if (-not (Test-Path -LiteralPath $key)) { return $false }
    if ([string]::IsNullOrEmpty($Value)) {
        Remove-ItemProperty -LiteralPath $key -Name $Name -ErrorAction SilentlyContinue
    } else {
        Set-ItemProperty -LiteralPath $key -Name $Name -Value $Value -Type String
    }
    return $true
}

function Test-FallowPathReadable {
    <#
    .SYNOPSIS
        True when this process can actually open the file for reading.
    .DESCRIPTION
        Test-Path answers from the directory entry and says nothing about the
        file's DACL. An install made in the target's own session leaves an
        owner-only agent.toml that an admin context cannot read; this is how that
        is detected before the installer tries to parse it.
        (exercised in CI on windows-latest - verify on target)
    #>
    param([Parameter(Mandatory)][string]$Path)
    try {
        $stream = [System.IO.File]::OpenRead($Path)
        $stream.Dispose()
        return $true
    } catch {
        return $false
    }
}
