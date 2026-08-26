#!/usr/bin/env bash
set -euo pipefail

# One artifact per desk: agentctl.exe, the Windows install scripts, and an
# operator README, hashed into a manifest and zipped. A desk needs this zip and
# its own join file, and nothing else - no repo checkout.
#
# The layout mirrors deploy/ on purpose. install.ps1 and fetch-llama.ps1 resolve
# agent.example.toml and the staged llama build from the parent of their own
# directory, and bootstrap.ps1 resolves windows\install.ps1 from beside itself,
# so shipping them at the same relative positions makes every path resolve from
# the unzipped bundle with no change to the scripts.
#
# build refuses to write over an existing bundle directory or zip, the same
# refusal bundle.sh makes: delete the old one, or build into another --output.
#
# The verify discipline is bundle.sh's, restated here rather than shared: each
# bundler carries its own verifier (deploy/bundle.ps1 does the same in
# PowerShell) so a bundle can be checked by the script that built it.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# <source under deploy/>:<destination in the bundle>
WINDOWS_FILES=(
    "SITE-BUNDLE.md:README.md"
    "agent.example.toml:agent.example.toml"
    "bootstrap.ps1:bootstrap.ps1"
    "windows/JOIN-README.md:windows/JOIN-README.md"
    "windows/doctor.ps1:windows/doctor.ps1"
    "windows/fallow-agent-task.xml:windows/fallow-agent-task.xml"
    "windows/fetch-llama.ps1:windows/fetch-llama.ps1"
    "windows/install.ps1:windows/install.ps1"
    "windows/lib/backend.ps1:windows/lib/backend.ps1"
    "windows/llama-manifest.psd1:windows/llama-manifest.psd1"
    "windows/new-site-config.ps1:windows/new-site-config.ps1"
    "windows/site-join.schema.json:windows/site-join.schema.json"
    "windows/uninstall.ps1:windows/uninstall.ps1"
)

die() { printf 'site-bundle: %s\n' "$*" >&2; exit 1; }
log() { printf 'site-bundle: %s\n' "$*" >&2; }

hash_file() {
    if command -v shasum >/dev/null; then shasum -a 256 "$1" | awk '{print $1}';
    elif command -v sha256sum >/dev/null; then sha256sum "$1" | awk '{print $1}';
    else die "shasum or sha256sum is required"; fi
}

validate_manifest_path() {
    case "$1" in
        /*|./*|*/./*|*/.|../*|*/../*|*/..|.|..|*//*) return 1 ;;
    esac
    [ -n "$1" ] && [ "$1" != "manifest.sha256" ]
}

verify_bundle() {
    local bundle="${1:?bundle directory is required}" line want path got count=0 actual
    while [ "$bundle" != "/" ] && [ "${bundle%/}" != "$bundle" ]; do
        bundle="${bundle%/}"
    done
    [ -f "${bundle}/manifest.sha256" ] || die "manifest.sha256 is missing"
    [ -z "$(find "$bundle" -type l -print -quit)" ] || die "bundle contains a symbolic link"
    [ -z "$(find "$bundle" ! -type f ! -type d -print -quit)" ] \
        || die "bundle contains an unsupported file type"
    while IFS= read -r line || [ -n "$line" ]; do
        [[ "$line" =~ ^([0-9a-f]{64})\ \ (.+)$ ]] || die "invalid manifest line"
        want="${BASH_REMATCH[1]}"; path="${BASH_REMATCH[2]}"
        validate_manifest_path "$path" || die "unsafe manifest path: $path"
        [ -f "${bundle}/${path}" ] || die "missing bundle file: $path"
        got="$(hash_file "${bundle}/${path}")"
        [ "$got" = "$want" ] || die "hash mismatch: $path"
        count=$((count + 1))
    done < "${bundle}/manifest.sha256"
    [ "$count" -gt 0 ] || die "manifest is empty"
    [ -z "$(cut -c 67- "${bundle}/manifest.sha256" | LC_ALL=C sort | uniq -d)" ] \
        || die "manifest contains duplicate paths"
    actual="$(find "$bundle" -type f ! -path "${bundle}/manifest.sha256" | wc -l | tr -d ' ')"
    [ "$count" -eq "$actual" ] || die "manifest does not cover every bundle file"
    log "verified ${count} files"
}

build_bundle() {
    local agent="" version="" output="dist" platform="windows-amd64" arg
    local entry source destination name stage bundle manifest_paths path digest
    local files=()
    while [ "$#" -gt 0 ]; do
        arg="$1"; shift
        case "$arg" in
            --agent) [ "$#" -gt 0 ] || die "--agent needs a path"; agent="$1"; shift ;;
            --version) [ "$#" -gt 0 ] || die "--version needs a value"; version="$1"; shift ;;
            --output) [ "$#" -gt 0 ] || die "--output needs a directory"; output="$1"; shift ;;
            --platform) [ "$#" -gt 0 ] || die "--platform needs a value"; platform="$1"; shift ;;
            *) die "unknown build option: $arg" ;;
        esac
    done
    [ -n "$agent" ] || die "build requires --agent <agentctl.exe>"
    [ -n "$version" ] || die "build requires --version <version>"
    [ -f "$agent" ] || die "no agent binary at $agent"
    case "$platform" in
        windows-amd64) files=("${WINDOWS_FILES[@]}") ;;
        *) die "unsupported platform: $platform (windows-amd64 only)" ;;
    esac
    command -v zip >/dev/null || die "zip is required"

    name="fallow-site-agent_${version}_${platform//-/_}"
    [ ! -e "${output}/${name}" ] || die "output already exists: ${output}/${name}"
    [ ! -e "${output}/${name}.zip" ] || die "output already exists: ${output}/${name}.zip"
    stage="$(mktemp -d)"; trap 'rm -rf "${stage}"' EXIT
    bundle="${stage}/${name}"
    for entry in "${files[@]}"; do
        source="${SCRIPT_DIR}/${entry%%:*}"; destination="${bundle}/${entry#*:}"
        [ -f "$source" ] || die "missing bundle source: ${entry%%:*}"
        mkdir -p "$(dirname "$destination")"
        cp "$source" "$destination"
    done
    cp "$agent" "${bundle}/agentctl.exe"

    manifest_paths="${stage}/manifest.paths"
    (cd "$bundle" && find . -type f ! -path ./manifest.sha256 -print \
        | sed 's#^./##' | LC_ALL=C sort > "$manifest_paths")
    while IFS= read -r path; do
        digest="$(hash_file "${bundle}/${path}")" \
            || die "could not hash bundle file: $path"
        printf '%s  %s\n' "$digest" "$path"
    done < "$manifest_paths" > "${bundle}/manifest.sha256"
    verify_bundle "$bundle"
    # -X drops the uid/gid and extra attributes, which say nothing to a Windows
    # desk. It does not make the zip reproducible: every entry still carries the
    # mtime of its staged copy, so two builds of the same tree differ byte for
    # byte. manifest.sha256 is what proves the contents, not the archive's bytes.
    (cd "$stage" && zip -q -r -X "${name}.zip" "$name")

    mkdir -p "$output"
    mv "$bundle" "${output}/${name}"
    mv "${stage}/${name}.zip" "${output}/${name}.zip"
    rm -rf "$stage"; trap - EXIT
    log "built ${output}/${name}.zip"
}

case "${1:-}" in
    build) shift; build_bundle "$@" ;;
    verify) shift; verify_bundle "${1:-}" ;;
    *) die "usage: site-bundle.sh {build|verify} [options]" ;;
esac
