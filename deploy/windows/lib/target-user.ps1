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
        # Inside the guard too: the reverse translation reaches the same name
        # service and fails the same ways (an unreachable domain controller, a
        # SID with no account behind it), and an operator needs the same message.
        $canonical = $sid.Translate([System.Security.Principal.NTAccount]).Value
    } catch {
        throw "[user] cannot resolve account '$Name'; pass an account that exists on this machine or in its domain (user, MACHINE\user or DOMAIN\user): $($_.Exception.Message)"
    }

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
        NTUSER.DAT is a file on disk: Get-FallowTargetEnvOffline mounts it to
        read the overrides, and nothing here can write to it.
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

function Invoke-FallowHiveLoad {
    <#
    .SYNOPSIS
        Mount a hive file under HKEY_USERS\<Mount>. Returns reg.exe's exit code.
    .DESCRIPTION
        One named wrapper per reg.exe call, so the mount and the release are each
        a seam the Pester suite can drive without a real hive and a real
        privilege. $LASTEXITCODE is read inside the guard: a throw would leave
        the previous command's code standing and a failed load would read as a
        good one.
        (exercised in CI on windows-latest - verify on target)
    #>
    param(
        [Parameter(Mandatory)][string]$Mount,
        [Parameter(Mandatory)][string]$HiveFile
    )
    try {
        $null = & reg.exe load "HKU\$Mount" $HiveFile 2>$null
        return $LASTEXITCODE
    } catch { return 1 }
}

function Invoke-FallowHiveUnload {
    <#
    .SYNOPSIS
        Release a hive mounted by Invoke-FallowHiveLoad. Returns reg.exe's exit
        code.
    .DESCRIPTION
        (exercised in CI on windows-latest - verify on target)
    #>
    param([Parameter(Mandatory)][string]$Mount)
    try {
        $null = & reg.exe unload "HKU\$Mount" 2>$null
        return $LASTEXITCODE
    } catch { return 1 }
}

function Get-FallowTargetEnvOffline {
    <#
    .SYNOPSIS
        Read User-scope environment variables for a signed-out account by
        mounting its NTUSER.DAT. Returns $null when the hive cannot be mounted.
    .DESCRIPTION
        Windows mounts a user's hive under HKEY_USERS only while they are signed
        in, which in an admin-context install is exactly when they are not. The
        hive is still a file in their profile, and an elevated context can mount
        it: reg.exe load attaches it to a private HKEY_USERS key, reg.exe unload
        releases it again.

        This exists for one value. FALLOW_STATE_PATH in the target's User scope
        relocates their enrolled identity; an installer that cannot see it looks
        at the default path, calls an enrolled desk fresh, and stages a join
        bundle whose live token the agent will never consume. The bind and
        join-bundle overrides are read from the same mount because they are the
        same blindness and cost nothing extra.

        Returns a hashtable of the requested names to their values, with unset
        names simply absent. Returns $null when the hive could not be mounted at
        all - NTUSER.DAT is held open while a profile is in a half-state - which
        is the caller's warning to give, not a failure here.

        The unload is the one part that is not best-effort: a hive left mounted
        stops the profile loading at that account's next logon. The registry
        provider caches the key handle it opened, so those handles are dropped
        before the unload and the unload is retried; hives release lazily. If
        every retry fails this throws rather than returning: the alternative was
        an installer that reported success over an account that can no longer log
        in.
        (exercised in CI on windows-latest - verify on target)
    #>
    param(
        [Parameter(Mandatory)][string]$ProfilePath,
        [Parameter(Mandatory)][string[]]$Names
    )
    $hiveFile = Join-Path $ProfilePath 'NTUSER.DAT'
    if (-not (Test-Path -LiteralPath $hiveFile -PathType Leaf)) { return $null }
    $mount = 'Fallow_' + [guid]::NewGuid().ToString('N')
    # A mount this account is not allowed to make, a hive another process holds
    # open, a file that is not a hive at all: every one of them is the same
    # answer here, and none of them may fail the install.
    if ((Invoke-FallowHiveLoad -Mount $mount -HiveFile $hiveFile) -ne 0) { return $null }

    try {
        $values = @{}
        $key = "Registry::HKEY_USERS\$mount\Environment"
        if (Test-Path -LiteralPath $key) {
            $item = Get-ItemProperty -LiteralPath $key -ErrorAction SilentlyContinue
            foreach ($name in $Names) {
                # Case-insensitive on purpose, like Get-FallowTargetEnv: Windows
                # environment variable names are. Key the result by the name the
                # caller asked for, which is what it will look up.
                if ($item -and ($item.PSObject.Properties.Name -contains $name)) {
                    $value = [string]$item.$name
                    if (-not [string]::IsNullOrEmpty($value)) { $values[$name] = $value }
                }
            }
        }
        return $values
    } finally {
        [gc]::Collect()
        [gc]::WaitForPendingFinalizers()
        $released = $false
        for ($attempt = 1; $attempt -le 5; $attempt++) {
            if ((Invoke-FallowHiveUnload -Mount $mount) -eq 0) { $released = $true; break }
            Start-Sleep -Milliseconds 200
        }
        # Fatal, not a warning. A warning let the installer go on to report
        # success while this account's NTUSER.DAT stayed mounted, and a mounted
        # hive stops their profile loading at the next logon - the install would
        # have locked out the person it was for, and said it worked. A deployment
        # tool must see this as the failure it is.
        if (-not $released) {
            throw "[user] the temporary registry mount HKU\$mount could not be released after 5 attempts, so $hiveFile is still mounted. That account's profile will NOT load at their next logon while it is. Close whatever holds it open (a reboot is the blunt fix), or unload it by hand: reg unload HKU\$mount. This install did not finish; re-run it once the mount is gone."
        }
    }
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
        True when this process can actually read the file or directory.
    .DESCRIPTION
        Test-Path answers from the directory entry and says nothing about the
        path's DACL. An install made in the target's own session leaves an
        owner-only agent.toml that an admin context cannot read, and an
        owner-only .fallow\site whose security descriptor it cannot even read;
        this is how both are detected before the installer parses the config or
        re-protects the directory. A directory is answered by reading that
        descriptor, which is what Protect-FallowSitePath needs and what an
        owner-only DACL denies; File.OpenRead cannot open one at all.
        (exercised in CI on windows-latest - verify on target)
    #>
    param([Parameter(Mandatory)][string]$Path)
    if (Test-Path -LiteralPath $Path -PathType Container) {
        try {
            [void](Get-Acl -LiteralPath $Path)
            return $true
        } catch {
            return $false
        }
    }
    try {
        $stream = [System.IO.File]::OpenRead($Path)
        $stream.Dispose()
        return $true
    } catch {
        return $false
    }
}
