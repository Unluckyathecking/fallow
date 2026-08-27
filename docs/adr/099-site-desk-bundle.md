# one artifact per Site Mode desk

## Status

Proposed

## Date

2026-08-26

## Goal

Make installing a Site Mode desk a download and one command. Today it takes four
things on the machine: a checkout of this repository for the deploy scripts, an
`agentctl.exe` from a GitHub Release, a llama.cpp build fetched by
`deploy\windows\fetch-llama.ps1`, and the per-desk join file. Three of those are
the same on every desk in the pilot and only one is per-desk, but the operator
assembles all four by hand on each machine, and a git checkout on a school PC is
a prerequisite nobody wants to defend to IT.

## Owned paths

- `deploy/site-bundle.sh` (new)
- `deploy/SITE-BUNDLE.md` (new)
- `deploy/README.md`
- `.github/workflows/release.yml`, `.github/workflows/ci.yml`
- `tests/deploy/test_site_bundle.py` (new)
- `docs/lan-site/operator-runbook.md` (§0 and §4)
- `docs/adr/099-site-desk-bundle.md`, `CHANGELOG.md`

No other path belongs to this change. No PowerShell script is edited: the point
of the layout below is that none needs to be.

## Decision

**One zip plus one join file.** Every release carries
`fallow-site-agent_<version>_windows_amd64.zip`, matching the GoReleaser naming
family. A desk unzips it, stages llama.cpp, and runs

```powershell
.\bootstrap.ps1 -JoinBundle <join file> -GoBinary .\agentctl.exe
```

Two arguments, both visible in the layout. A wrapper script that filled them in
was considered and dropped: it would hide the only two paths the operator has to
get right, to save typing them once per desk. `bootstrap.ps1` is not that
wrapper (it takes the same two arguments and adds the machine report and the
self-test), and `.\windows\install.ps1` with those arguments stays the exact
same install for anyone who wants no layer at all.

**The bundle mirrors `deploy/`.** `agentctl.exe`, `agent.example.toml` and the
generated `README.md` at the root; the scripts under `windows/`. That is not
cosmetic. `install.ps1` and `fetch-llama.ps1` resolve `agent.example.toml` and
the staged `bin\windows\` llama build from the parent of their own directory,
and dot-source `lib\backend.ps1` and `new-site-config.ps1` from beside
themselves. Mirroring the repository shape makes all eleven of those relative
references land with no change to any script, so the bundle cannot drift from
the checkout it was cut from. A test asserts that closure on a built bundle
rather than trusting it.

**What is in it**: the agent binary, `bootstrap.ps1`, `install.ps1`,
`uninstall.ps1`, `doctor.ps1`, `fetch-llama.ps1`, `new-site-config.ps1`,
`lib\backend.ps1`, `fallow-agent-task.xml`, `llama-manifest.psd1`,
`site-join.schema.json`, `JOIN-README.md`, `agent.example.toml`, an operator
`README.md`, and `manifest.sha256`.

`bootstrap.ps1` is in the bundle for the same reason the layout mirrors
`deploy/`: it resolves `windows\install.ps1` from beside itself, so it lands at
the bundle root and works unchanged. It is worth its 6 KB because it is the only
thing that reports the desk's RAM and GPU and warns before the install (a desk
with no NVIDIA GPU cannot run the pinned CUDA build, and finding that out from
`bootstrap.ps1` beats finding it out from a crash loop), and the only thing that
runs the post-install self-test. Both documented paths (the bundle and a
checkout) are now the same command.

**What is not**: no model weights, and no llama.cpp. The CUDA build and its
runtime DLLs are larger than everything else here together, they are already
pinned and hash-checked by `fetch-llama.ps1` against `llama-manifest.psd1`, and
a desk either has internet for that fetch or is handed a staged copy. The
bundle README says both. No join file either: that is per-desk, it is a
credential, and a bundle carrying one would be a bundle that must not be copied.

**`manifest.sha256`, verified by the builder.** Same discipline as
`deploy/bundle.sh`: `build | verify` verbs, every file hashed, and `verify`
rejects a changed file, an unlisted file, an unsafe or duplicate manifest path,
a symbolic link and anything that is not a regular file. That verifier is
restated in `site-bundle.sh` rather than shared with `bundle.sh`: the house
already has two copies of it, one per bundle format (`bundle.ps1` is the
PowerShell one), and a bundle should be checkable by the script that built it.

**Built from the released binary, not a rebuild.** On a tag, the release job
downloads the published `fallow-agentctl_<version>_windows_amd64.zip`, takes
`agentctl.exe` out of it, and bundles that, so the binary a desk installs is
byte-for-byte the one `checksums.txt` already covers. The bundle's own line is
appended to `checksums.txt` the way the darwin archive's is
([ADR 098](098-go-idle-fail-closed.md)), filtering any stale line first so a
re-run cannot list the same archive twice. That job runs after `release-macos`,
not beside it: both rewrite `checksums.txt`, and the later writer would drop the
other's line.

## Verification

`tests/deploy/test_site_bundle.py` builds a bundle in a temporary directory and
asserts the zip holds the bootstrap, the installer, the agent and the manifest;
that `verify` accepts it; that all five refusals fire: a tampered file, an
unlisted file, an unsafe manifest path, a duplicate manifest path, a symlink and
a non-regular file; and
that every `$ScriptDir`/`$DeployDir`-relative reference in every bundled script
resolves inside the layout. That last one is the regression this change can
actually be broken by: adding a dot-source to `install.ps1` without adding the
file to the bundle breaks a desk and nothing else would notice.

On pull requests the release workflow bundles the GoReleaser snapshot binary and
uploads the zip; `ci.yml` builds and verifies a bundle on every push, the way
`offline-bundle` continuously proves `bundle.sh`. Every one of those three
verifications unzips the archive and verifies what came out of it, not the
staging tree it was zipped from: the staging tree is what the builder already
checked, and the zip is what a desk actually receives.

## Compatibility

Additive. The repository checkout path is unchanged and stays documented as the
development route: `deploy\bootstrap.ps1` still detects the machine and hands
off to the same `install.ps1`, and no script it calls was touched.

## Exclusions and honest gaps

**The bundle is unsigned.** No Authenticode on `agentctl.exe`, no signature on
the zip; `manifest.sha256` proves integrity to someone who already trusts the
copy they hold, and nothing more. SmartScreen and endpoint-protection
allowlisting remain an IT prerequisite with the lead time
`docs/pilot/it-checklist.md` describes. Signing is the next thing worth doing
here.

**Windows only.** `--platform` takes `windows-amd64` and rejects everything
else. macOS desks are not the Site Mode pilot target, and the file list and
naming are already keyed by platform, so darwin is a list and a case arm when a
macOS site exists, not a shape change.

**A failed macOS job holds up the desk bundle.** `release-site-bundle` needs
`release-macos`, because both rewrite `checksums.txt` and the later writer would
drop the other's line. The cost of that ordering is that a flaky macOS runner
leaves a release with no desk bundle attached even though the Windows archive
published fine. Recovery is to re-run the failed job from the Actions run once
the runner is healthy. Both jobs strip their own stale line from `checksums.txt`
before appending, so a re-run is safe and never lists an archive twice. Nothing
needs re-tagging.

**Not executed on Windows.** This was authored in a Linux sandbox with no
PowerShell and no Windows host. What is verified is that the bundle assembles,
verifies, tampers detectably, and carries every file the scripts reference by a
relative path. That `install.ps1` then runs green from the unzipped bundle on a
real desk is a pilot-day check, exactly as it was before this change.
