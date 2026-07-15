#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
LLAMA_RELEASE="b4589"
CUDA_TAG="cu12.4"
MAC_ASSET="llama-${LLAMA_RELEASE}-bin-macos-arm64.zip"
WIN_ASSET="llama-${LLAMA_RELEASE}-bin-win-cuda-${CUDA_TAG}-x64.zip"
CUDART_ASSET="cudart-llama-bin-win-${CUDA_TAG}-x64.zip"
BASE_URL="https://github.com/ggml-org/llama.cpp/releases/download/${LLAMA_RELEASE}"

die() { printf 'bundle: %s\n' "$*" >&2; exit 1; }
log() { printf 'bundle: %s\n' "$*" >&2; }
need() { command -v "$1" >/dev/null || die "$1 is required"; }

hash_file() {
    if command -v shasum >/dev/null; then shasum -a 256 "$1" | awk '{print $1}';
    elif command -v sha256sum >/dev/null; then sha256sum "$1" | awk '{print $1}';
    else die "shasum or sha256sum is required"; fi
}

validate_manifest_path() {
    case "$1" in
        /*|./*|*/./*|*/.|../*|*/../*|*/..|.|*//*) return 1 ;;
    esac
    [ -n "$1" ] && [ "$1" != "manifest.sha256" ]
}

verify_bundle() {
    local bundle="${1:?bundle directory is required}" line want path got count=0 actual
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
    actual="$(find "$bundle" -type f ! -name manifest.sha256 | wc -l | tr -d ' ')"
    [ "$count" -eq "$actual" ] || die "manifest does not cover every bundle file"
    log "verified ${count} files"
}

download() {
    curl --fail --location --proto '=https' --tlsv1.2 --output "$2" "$1"
}

build_bundle() {
    local output="" models="" arg stage archive mac_server
    while [ "$#" -gt 0 ]; do
        arg="$1"; shift
        case "$arg" in
            --output) [ "$#" -gt 0 ] || die "--output needs a directory"; output="$1"; shift ;;
            --with-models) [ "$#" -gt 0 ] || die "--with-models needs a directory"; models="$1"; shift ;;
            *) die "unknown build option: $arg" ;;
        esac
    done
    [ -n "$output" ] || die "build requires --output"
    [ ! -e "$output" ] || die "output already exists: $output"
    need uv; need curl; need unzip
    stage="$(mktemp -d)"; trap 'rm -rf "${stage}"' EXIT
    mkdir -p "${stage}/bundle/wheels/workspace" "${stage}/bundle/wheels/macos-arm64" \
        "${stage}/bundle/wheels/windows-x64" "${stage}/bundle/llama/macos-arm64" \
        "${stage}/bundle/llama/windows-x64-cuda" "${stage}/bundle/config" \
        "${stage}/bundle/models" "${stage}/archives"

    (cd "$ROOT_DIR" && uv export --frozen --no-dev --no-emit-workspace \
        --format requirements-txt > "${stage}/bundle/requirements.lock.txt")
    (cd "$ROOT_DIR" && uv export --frozen --no-dev --no-emit-workspace \
        --format requirements-txt --no-hashes --no-annotate --no-header \
        > "${stage}/requirements.export.txt")
    (cd "$ROOT_DIR" && uv run python deploy/filter_bundle_requirements.py \
        "${stage}/requirements.export.txt" "${stage}/requirements.macos.txt" \
        --target macos-arm64)
    (cd "$ROOT_DIR" && uv run python deploy/filter_bundle_requirements.py \
        "${stage}/requirements.export.txt" "${stage}/requirements.windows.txt" \
        --target windows-x64)
    (cd "$ROOT_DIR" && uv build --all-packages --wheel \
        --out-dir "${stage}/bundle/wheels/workspace")
    (cd "$ROOT_DIR" && uv run --with pip python -m pip download --only-binary=:all: \
        --platform macosx_11_0_arm64 --python-version 312 --implementation cp --abi cp312 \
        --dest "${stage}/bundle/wheels/macos-arm64" -r "${stage}/requirements.macos.txt")
    (cd "$ROOT_DIR" && uv run --with pip python -m pip download --only-binary=:all: \
        --platform win_amd64 --python-version 312 --implementation cp --abi cp312 \
        --dest "${stage}/bundle/wheels/windows-x64" -r "${stage}/requirements.windows.txt")

    archive="${stage}/archives/${MAC_ASSET}"; download "${BASE_URL}/${MAC_ASSET}" "$archive"
    mkdir -p "${stage}/macos-unpacked"
    unzip -q "$archive" -d "${stage}/macos-unpacked"
    mac_server="$(find "${stage}/macos-unpacked" -type f -name llama-server -print -quit)"
    [ -n "$mac_server" ] || die "macOS archive does not contain llama-server"
    cp -R "$(dirname "$mac_server")/." "${stage}/bundle/llama/macos-arm64/"
    chmod +x "${stage}/bundle/llama/macos-arm64/llama-server"
    archive="${stage}/archives/${WIN_ASSET}"; download "${BASE_URL}/${WIN_ASSET}" "$archive"
    unzip -q "$archive" -d "${stage}/bundle/llama/windows-x64-cuda"
    archive="${stage}/archives/${CUDART_ASSET}"; download "${BASE_URL}/${CUDART_ASSET}" "$archive"
    unzip -q -o "$archive" -d "${stage}/bundle/llama/windows-x64-cuda"
    [ -x "${stage}/bundle/llama/macos-arm64/llama-server" ] \
        || die "macOS bundle does not contain executable llama-server"
    find "${stage}/bundle/llama/windows-x64-cuda" -type f -name llama-server.exe -print -quit | grep -q . \
        || die "Windows archive does not contain llama-server.exe"
    find "${stage}/bundle/llama/windows-x64-cuda" -type f -iname 'cudart64_*.dll' -print -quit | grep -q . \
        || die "Windows bundle does not contain cudart DLLs"

    cp "${SCRIPT_DIR}/agent.example.toml" "${stage}/bundle/config/agent.toml"
    cp "${SCRIPT_DIR}/coordinator.example.toml" "${stage}/bundle/config/coordinator.toml"
    cp "${SCRIPT_DIR}/bundle.sh" "${stage}/bundle/install.sh"
    cp "${SCRIPT_DIR}/bundle.ps1" "${stage}/bundle/install.ps1"
    cp "${SCRIPT_DIR}/OFFLINE.md" "${stage}/bundle/README.md"
    cp "${ROOT_DIR}/uv.lock" "${stage}/bundle/uv.lock"
    if [ -n "$models" ]; then
        [ -d "$models" ] || die "model directory does not exist: $models"
        [ -z "$(find "$models" -type l -print -quit)" ] \
            || die "model directory contains a symbolic link"
        cp -R "${models}/." "${stage}/bundle/models/"
    fi
    (cd "${stage}/bundle" && find . -type f ! -name manifest.sha256 -print | sed 's#^./##' | \
        LC_ALL=C sort | while IFS= read -r path; do printf '%s  %s\n' "$(hash_file "$path")" "$path"; done \
        > manifest.sha256)
    verify_bundle "${stage}/bundle"
    mkdir -p "$(dirname "$output")"; mv "${stage}/bundle" "$output"
    rm -rf "$stage"; trap - EXIT
    log "built $output"
}

install_bundle() {
    local bundle="$SCRIPT_DIR" prefix="${HOME}/.fallow/offline" dry=0 target="macos-arm64" arg
    local python="${FALLOW_PYTHON:-python3}"
    while [ "$#" -gt 0 ]; do
        arg="$1"; shift
        case "$arg" in
            --bundle) bundle="$1"; shift ;;
            --prefix) prefix="$1"; shift ;;
            --target) target="$1"; shift ;;
            --dry-run) dry=1 ;;
            *) die "unknown install option: $arg" ;;
        esac
    done
    [ "$target" = "macos-arm64" ] || die "shell install supports macos-arm64"
    verify_bundle "$bundle"
    if [ "$dry" -eq 1 ]; then
        printf 'Would create %s and install locked wheels, llama.cpp, models, and config.\n' "$prefix"
        return
    fi
    command -v "$python" >/dev/null || die "$python is required"
    "$python" -c 'import sys; raise SystemExit(sys.version_info[:2] != (3, 12))' \
        || die "Python 3.12 is required"
    mkdir -p "$prefix"; "$python" -m venv "${prefix}/venv"
    "${prefix}/venv/bin/python" -m pip install --no-index \
        --find-links "${bundle}/wheels/workspace" --find-links "${bundle}/wheels/macos-arm64" \
        fallow-agent fallow-bench fallow-coordinator fallow-cli
    cp -R "${bundle}/llama/macos-arm64" "${prefix}/llama"
    cp -R "${bundle}/models" "${prefix}/models"
    if [ ! -f "${prefix}/agent.toml" ]; then
        "${prefix}/venv/bin/python" - \
            "${bundle}/config/agent.toml" "${prefix}/agent.toml" \
            "${prefix}/llama/llama-server" <<'PY'
from pathlib import Path
import sys

source, destination, llama = map(Path, sys.argv[1:])
lines = source.read_text(encoding="utf-8").splitlines()
rendered = [
    f'llama_server_binary = "{llama}"' if line.startswith("llama_server_binary = ") else line
    for line in lines
]
destination.write_text("\n".join(rendered) + "\n", encoding="utf-8")
PY
    fi
    log "installed to $prefix"
}

case "${1:-}" in
    build) shift; build_bundle "$@" ;;
    verify) shift; verify_bundle "${1:-$SCRIPT_DIR}" ;;
    install) shift; install_bundle "$@" ;;
    *) die "usage: bundle.sh {build|verify|install} [options]" ;;
esac
