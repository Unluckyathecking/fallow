#!/usr/bin/env bash
# fetch-llama.sh — download a PINNED llama.cpp release for macOS (Apple Silicon)
# and unpack it into deploy/bin/macos/.
#
# llama.cpp does NOT publish a per-asset SHA256SUMS file on its GitHub releases,
# so this script cannot verify against an upstream checksum. Instead it records
# the SHA256 of what it actually downloaded into deploy/llama-version.lock and
# refuses to proceed on a subsequent run if the hash of the pinned asset drifts
# from the locked value (protects against a re-tagged / mutated asset).
#
# HONESTY: this script was authored in a sandbox with no network access. The
# release tag and the exact GitHub asset name MUST be verified against
# https://github.com/ggml-org/llama.cpp/releases before first use.
# Every network-dependent step is marked (untested — verify on target).
set -euo pipefail

# ── Pinned release (single source of truth) ─────────────────────────────────
# Bump these two values together to move to a new llama.cpp build.
LLAMA_RELEASE="b4589"          # (untested — verify tag exists on releases page)
LLAMA_MACOS_ASSET="llama-${LLAMA_RELEASE}-bin-macos-arm64.zip"

GITHUB_REPO="ggml-org/llama.cpp"
BASE_URL="https://github.com/${GITHUB_REPO}/releases/download/${LLAMA_RELEASE}"

# ── Paths ───────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${SCRIPT_DIR}/bin/macos"
LOCK_FILE="${SCRIPT_DIR}/llama-version.lock"
LLAMA_BINARY_NAME="llama-server"

log()  { printf '[fetch-llama] %s\n' "$*" >&2; }
die()  { printf '[fetch-llama] ERROR: %s\n' "$*" >&2; exit 1; }

# ── Preconditions ───────────────────────────────────────────────────────────
[ "$(uname -s)" = "Darwin" ] || die "this fetcher is macOS-only; use deploy/windows/fetch-llama.ps1 on Windows"
[ "$(uname -m)" = "arm64" ]  || die "pinned asset is macos-arm64 only; Intel Macs are unsupported in v0.1"
command -v curl >/dev/null   || die "curl is required"
command -v shasum >/dev/null || die "shasum is required"
command -v unzip >/dev/null  || die "unzip is required"

mkdir -p "${BIN_DIR}"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

archive="${TMP_DIR}/${LLAMA_MACOS_ASSET}"
url="${BASE_URL}/${LLAMA_MACOS_ASSET}"

log "downloading ${url}  (untested — verify on target)"
curl --fail --location --proto '=https' --tlsv1.2 \
     --output "${archive}" "${url}" \
  || die "download failed — verify LLAMA_RELEASE=${LLAMA_RELEASE} and asset name on the releases page"

got_hash="$(shasum -a 256 "${archive}" | awk '{print $1}')"
log "downloaded sha256=${got_hash}"

# ── Lockfile: record on first run, verify on subsequent runs ─────────────────
lock_key="${LLAMA_RELEASE}/${LLAMA_MACOS_ASSET}"
if [ -f "${LOCK_FILE}" ] && grep -q "^${lock_key} " "${LOCK_FILE}"; then
    want_hash="$(grep "^${lock_key} " "${LOCK_FILE}" | awk '{print $2}')"
    [ "${want_hash}" = "${got_hash}" ] \
      || die "hash mismatch for ${lock_key}: locked ${want_hash} != downloaded ${got_hash}"
    log "hash matches lockfile"
else
    printf '%s %s\n' "${lock_key}" "${got_hash}" >> "${LOCK_FILE}"
    log "recorded ${lock_key} -> ${got_hash} in ${LOCK_FILE}"
fi

# ── Unpack ──────────────────────────────────────────────────────────────────
log "unpacking into ${BIN_DIR}"
unzip -o -q "${archive}" -d "${TMP_DIR}/unpacked"

# The macOS archive layout has varied across releases (files at the root vs.
# under build/bin/). Locate the server binary wherever it landed.
src_binary="$(find "${TMP_DIR}/unpacked" -type f -name "${LLAMA_BINARY_NAME}" -perm -u+x -print -quit || true)"
[ -n "${src_binary}" ] || die "'${LLAMA_BINARY_NAME}' not found in archive — inspect ${archive} layout (untested — verify on target)"

# Copy the whole directory holding the binary so bundled dylibs travel with it.
src_root="$(dirname "${src_binary}")"
cp -R "${src_root}/." "${BIN_DIR}/"
chmod +x "${BIN_DIR}/${LLAMA_BINARY_NAME}"

log "installed llama-server -> ${BIN_DIR}/${LLAMA_BINARY_NAME}"
log "point supervisor.llama_binary at that path in ~/.fallow/agent.toml"
log "done"
