#!/usr/bin/env bash
# install.sh — install the Fallow agent as a per-user launchd LaunchAgent on macOS.
#
# v0.1 install story (deliberately the simplest honest one): Fallow is NOT on
# PyPI, so we assume a git checkout of the fallow monorepo already exists on this
# machine. We create a uv-managed virtualenv inside that checkout, then load a
# LaunchAgent that runs `.venv/bin/python -m fallow_agent run`.
#
# Prerequisites (see deploy/README.md):
#   - a git checkout of the fallow repo (pass its path or set FALLOW_REPO)
#   - uv installed (https://docs.astral.sh/uv/)
#   - Tailscale up; the agent config binds replicas to the tailnet IP
#   - deploy/bin/macos/llama-server present (run deploy/fetch-llama.sh first)
#
# HONESTY: authored in a sandbox. The launchctl bootstrap / venv build steps are
# marked (untested — verify on target).
set -euo pipefail

log() { printf '[install] %s\n' "$*" >&2; }
die() { printf '[install] ERROR: %s\n' "$*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${DEPLOY_DIR}/.." && pwd)"

# ── Resolve the fallow checkout to build the venv in ────────────────────────
# Default to the repo this script lives in; override with $1 or $FALLOW_REPO.
FALLOW_REPO="${1:-${FALLOW_REPO:-${REPO_ROOT}}}"
[ -f "${FALLOW_REPO}/pyproject.toml" ] || die "no pyproject.toml at ${FALLOW_REPO}; pass the fallow checkout path as the first argument"

LABEL="com.fallow.agent"
FALLOW_HOME="${HOME}/.fallow"
LOG_DIR="${FALLOW_HOME}/logs"
CONFIG_DST="${FALLOW_HOME}/agent.toml"
CONFIG_SRC="${DEPLOY_DIR}/agent.example.toml"   # created by the config module (I2)
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
PLIST_TEMPLATE="${SCRIPT_DIR}/${LABEL}.plist"
PLIST_DST="${LAUNCH_AGENTS_DIR}/${LABEL}.plist"

[ "$(uname -s)" = "Darwin" ] || die "install.sh is macOS-only"
command -v uv >/dev/null || die "uv is required (https://docs.astral.sh/uv/)"
[ -f "${PLIST_TEMPLATE}" ] || die "missing plist template ${PLIST_TEMPLATE}"

mkdir -p "${FALLOW_HOME}" "${LOG_DIR}" "${LAUNCH_AGENTS_DIR}"

# ── venv via uv (installs the workspace, incl. fallow-agent) ─────────────────
log "building uv venv in ${FALLOW_REPO}  (untested — verify on target)"
( cd "${FALLOW_REPO}" && uv sync --no-dev )
PYTHON_BIN="${FALLOW_REPO}/.venv/bin/python"
[ -x "${PYTHON_BIN}" ] || die "expected venv python at ${PYTHON_BIN} after 'uv sync'"

# ── config: copy the example on first install, never clobber a live one ──────
if [ -f "${CONFIG_DST}" ]; then
    log "keeping existing config ${CONFIG_DST}"
elif [ -f "${CONFIG_SRC}" ]; then
    cp "${CONFIG_SRC}" "${CONFIG_DST}"
    log "copied example config -> ${CONFIG_DST} (EDIT IT: enrollment token, coordinator URL, tailnet bind_host, llama_binary path)"
else
    log "WARNING: no config at ${CONFIG_DST} and no example at ${CONFIG_SRC}; create ${CONFIG_DST} before the agent will start"
fi

# ── render the plist template ────────────────────────────────────────────────
log "writing LaunchAgent ${PLIST_DST}"
sed \
  -e "s#__PYTHON__#${PYTHON_BIN}#g" \
  -e "s#__CONFIG__#${CONFIG_DST}#g" \
  -e "s#__STDOUT__#${LOG_DIR}/agent.out.log#g" \
  -e "s#__STDERR__#${LOG_DIR}/agent.err.log#g" \
  -e "s#__WORKDIR__#${FALLOW_REPO}#g" \
  "${PLIST_TEMPLATE}" > "${PLIST_DST}"

# ── (re)load into the user's GUI session ─────────────────────────────────────
# bootout first so re-running install.sh picks up a changed plist idempotently.
DOMAIN="gui/$(id -u)"
log "loading into ${DOMAIN}  (untested — verify on target)"
launchctl bootout "${DOMAIN}/${LABEL}" 2>/dev/null || true
launchctl bootstrap "${DOMAIN}" "${PLIST_DST}"
launchctl enable "${DOMAIN}/${LABEL}"
launchctl kickstart -k "${DOMAIN}/${LABEL}" || true

log "installed. logs: ${LOG_DIR}/agent.{out,err}.log"
log "status: launchctl print ${DOMAIN}/${LABEL}"
log "uninstall: deploy/macos/uninstall.sh"
