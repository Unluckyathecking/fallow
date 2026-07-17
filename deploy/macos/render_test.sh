#!/usr/bin/env bash
# render_test.sh — assert install.sh wires the right agent into the LaunchAgent
# and that the SHA256 verifier holds the trust boundary.
#
# Uses install.sh's FALLOW_INSTALL_DRY_RUN seam, which renders the plist and
# exits before touching uv, the binary copy, or launchctl. With --go-binary the
# service must run `agentctl run -config`; without it, `python -m fallow_agent
# run --config`. FALLOW_INSTALL_BACKEND forces the arch branch so both backends
# render on one host. install.sh is macOS-only (it guards on `uname -s`), so this
# skips elsewhere rather than failing.
set -euo pipefail

if [ "$(uname -s)" != "Darwin" ]; then
    echo "[render_test] SKIP: install.sh is macOS-only" >&2
    exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL="${SCRIPT_DIR}/install.sh"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

fail() { echo "[render_test] FAIL: $*" >&2; exit 1; }

# ── Go flavour: agentctl run -config, no interpreter args ────────────────────
go_plist="$(FALLOW_INSTALL_DRY_RUN=1 bash "${INSTALL}" --go-binary /bin/ls)"
echo "${go_plist}" | grep -q '<string>run</string>'      || fail "go: missing 'run'"
echo "${go_plist}" | grep -q '<string>-config</string>'  || fail "go: missing '-config'"
echo "${go_plist}" | grep -q 'bin/agentctl</string>'     || fail "go: not pointed at agentctl"
echo "${go_plist}" | grep -q 'fallow_agent'              && fail "go: still runs the Python module"

# ── Empty --go-binary: rejected, not silently the Python flavour ─────────────
# An operator passing an empty path meant the Go flavour and must get an error.
if FALLOW_INSTALL_DRY_RUN=1 bash "${INSTALL}" --go-binary= >/dev/null 2>&1; then
    fail "empty --go-binary should be rejected, not fall through to Python"
fi

# ── Python flavour (default): python -m fallow_agent run --config ────────────
# The dry-run render never invokes uv, but install.sh's Python flavour guards on
# uv before the render seam, so skip this leg cleanly when uv is absent rather
# than reporting a pass it never checked.
if ! command -v uv >/dev/null 2>&1; then
    echo "[render_test] SKIP python leg: uv is not installed" >&2
else
    py_plist="$(FALLOW_INSTALL_DRY_RUN=1 bash "${INSTALL}" "${REPO_ROOT}")"
    echo "${py_plist}" | grep -q '<string>fallow_agent</string>' || fail "python: missing fallow_agent"
    echo "${py_plist}" | grep -q '<string>--config</string>'     || fail "python: missing '--config'"
    echo "${py_plist}" | grep -q '.venv/bin/python</string>'     || fail "python: not pointed at the venv"
fi

# ── Backend selection: arch picks the llama-server build + thread policy ─────
# Force each branch with FALLOW_INSTALL_BACKEND and read the rendered env block.
omp_of() { echo "$1" | awk '/<key>OMP_NUM_THREADS<\/key>/{getline; gsub(/[^0-9]/,""); print; exit}'; }

metal_plist="$(FALLOW_INSTALL_DRY_RUN=1 FALLOW_INSTALL_BACKEND=metal bash "${INSTALL}" --go-binary /bin/ls)"
echo "${metal_plist}" | grep -q '/bin/macos/llama-server</string>'     || fail "metal: not pointed at the Metal build"
echo "${metal_plist}" | grep -q 'FALLOW_LLAMA_SERVER_BINARY'           || fail "metal: missing binary env var"

cpu_plist="$(FALLOW_INSTALL_DRY_RUN=1 FALLOW_INSTALL_BACKEND=cpu bash "${INSTALL}" --go-binary /bin/ls)"
echo "${cpu_plist}" | grep -q '/bin/macos-x64/llama-server</string>'   || fail "cpu: not pointed at the CPU build"

metal_omp="$(omp_of "${metal_plist}")"
cpu_omp="$(omp_of "${cpu_plist}")"
[ -n "${cpu_omp}" ] && [ "${cpu_omp}" -ge 1 ]     || fail "cpu: OMP_NUM_THREADS not a positive integer"
[ "${cpu_omp}" -le "${metal_omp}" ]               || fail "cpu: thread cap (${cpu_omp}) not conservative vs metal (${metal_omp})"

# ── SHA256 verifier: matches pass, mismatch and missing entry fail closed ─────
verify="${SCRIPT_DIR}/verify-sha256.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT
printf 'fallow-agent\n' > "${tmp}/llama-server"
good="$(shasum -a 256 "${tmp}/llama-server" | awk '{print $1}')"

printf '%s  llama-server\n' "${good}" > "${tmp}/manifest.sha256"
sh "${verify}" "${tmp}/llama-server" "${tmp}/manifest.sha256" >/dev/null 2>&1 || fail "verifier: rejected a matching hash"

printf '%s  llama-server\n' "0000000000000000000000000000000000000000000000000000000000000000" > "${tmp}/bad.sha256"
if sh "${verify}" "${tmp}/llama-server" "${tmp}/bad.sha256" >/dev/null 2>&1; then
    fail "verifier: accepted a mismatched hash"
fi

: > "${tmp}/empty.sha256"
if sh "${verify}" "${tmp}/llama-server" "${tmp}/empty.sha256" >/dev/null 2>&1; then
    fail "verifier: accepted a file with no manifest entry"
fi

echo "[render_test] OK: install paths, backend selection, and SHA256 gate all hold" >&2
