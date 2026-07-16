#!/usr/bin/env bash
# render_test.sh — assert install.sh wires the right agent into the LaunchAgent.
#
# Uses install.sh's FALLOW_INSTALL_DRY_RUN seam, which renders the plist and
# exits before touching uv, the binary copy, or launchctl. With --go-binary the
# service must run `agentctl run -config`; without it, `python -m fallow_agent
# run --config`. install.sh is macOS-only (it guards on `uname -s`), so this
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

# ── Python flavour (default): python -m fallow_agent run --config ────────────
py_plist="$(FALLOW_INSTALL_DRY_RUN=1 bash "${INSTALL}" "${REPO_ROOT}")"
echo "${py_plist}" | grep -q '<string>fallow_agent</string>' || fail "python: missing fallow_agent"
echo "${py_plist}" | grep -q '<string>--config</string>'     || fail "python: missing '--config'"
echo "${py_plist}" | grep -q '.venv/bin/python</string>'     || fail "python: not pointed at the venv"

echo "[render_test] OK: both install paths wire the expected agent" >&2
