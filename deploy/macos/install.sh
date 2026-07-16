#!/usr/bin/env bash
# install.sh — install the Fallow agent as a per-user launchd LaunchAgent on macOS.
#
# Two flavours share the same LaunchAgent, config, and launchctl wiring:
#
#   1. Python agent (default). Fallow is NOT on PyPI, so we assume a git checkout
#      of the fallow monorepo exists on this machine. We build a uv-managed venv
#      inside it and run `.venv/bin/python -m fallow_agent run`.
#
#   2. Prebuilt Go binary (`--go-binary <path>`). Point the LaunchAgent at a
#      released `agentctl` binary instead. This skips uv/venv entirely: it copies
#      the binary into ~/.fallow/bin and wires the plist to `agentctl run`.
#
# Prerequisites (see deploy/README.md):
#   - Python flavour: a git checkout of the fallow repo + uv (https://docs.astral.sh/uv/)
#   - Go flavour: a prebuilt agentctl binary (a GitHub Release archive, or `go build`)
#   - Both: Tailscale up; the agent config binds replicas to the tailnet IP;
#     deploy/bin/macos/llama-server present (run deploy/fetch-llama.sh first)
#
# HONESTY: authored in a sandbox. The launchctl bootstrap / venv build / binary
# install steps are marked (untested — verify on target).
#
# FALLOW_INSTALL_DRY_RUN=1 prints the rendered plist and exits before touching
# the system (uv, the binary copy, launchctl). Used by the render test.
set -euo pipefail

log() { printf '[install] %s\n' "$*" >&2; }
die() { printf '[install] ERROR: %s\n' "$*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${DEPLOY_DIR}/.." && pwd)"

# ── Parse arguments ─────────────────────────────────────────────────────────
# Positional arg (optional) = the fallow checkout for the Python flavour.
# --go-binary <path>        = install that prebuilt binary as the agent instead.
GO_BINARY=""
POSITIONAL=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --go-binary) [ "$#" -ge 2 ] || die "--go-binary requires a path"; [ -n "$2" ] || die "--go-binary requires a non-empty path"; GO_BINARY="$2"; shift 2 ;;
        --go-binary=*) GO_BINARY="${1#*=}"; [ -n "${GO_BINARY}" ] || die "--go-binary requires a non-empty path"; shift ;;
        --) shift; break ;;
        -*) die "unknown option: $1" ;;
        *) [ -z "${POSITIONAL}" ] || die "unexpected argument: $1"; POSITIONAL="$1"; shift ;;
    esac
done

DRY_RUN="${FALLOW_INSTALL_DRY_RUN:-0}"

LABEL="com.fallow.agent"
FALLOW_HOME="${HOME}/.fallow"
LOG_DIR="${FALLOW_HOME}/logs"
CONFIG_DST="${FALLOW_HOME}/agent.toml"
CONFIG_SRC="${DEPLOY_DIR}/agent.example.toml"   # created by the config module (I2)
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
PLIST_TEMPLATE="${SCRIPT_DIR}/${LABEL}.plist"
PLIST_DST="${LAUNCH_AGENTS_DIR}/${LABEL}.plist"
AGENT_BIN_DIR="${FALLOW_HOME}/bin"
AGENT_BIN="${AGENT_BIN_DIR}/agentctl"

[ "$(uname -s)" = "Darwin" ] || die "install.sh is macOS-only"
[ -f "${PLIST_TEMPLATE}" ] || die "missing plist template ${PLIST_TEMPLATE}"

# ── Select the agent flavour ────────────────────────────────────────────────
# PROGRAM/WORKDIR are the only per-flavour differences the plist needs; the Go
# path additionally rewrites the arg vector at render time (see render_plist).
if [ -n "${GO_BINARY}" ]; then
    [ -f "${GO_BINARY}" ] || die "no binary at ${GO_BINARY}"
    PROGRAM="${AGENT_BIN}"
    WORKDIR="${FALLOW_HOME}"
else
    # Default to the repo this script lives in; override with $1 or $FALLOW_REPO.
    FALLOW_REPO="${POSITIONAL:-${FALLOW_REPO:-${REPO_ROOT}}}"
    [ -f "${FALLOW_REPO}/pyproject.toml" ] || die "no pyproject.toml at ${FALLOW_REPO}; pass the fallow checkout path as the first argument"
    command -v uv >/dev/null || die "uv is required (https://docs.astral.sh/uv/)"
    PROGRAM="${FALLOW_REPO}/.venv/bin/python"
    WORKDIR="${FALLOW_REPO}"
fi

# ── Render the plist template ────────────────────────────────────────────────
# The template ships the Python arg vector (`-m fallow_agent run --config`). For
# the Go flavour we drop the `-m fallow_agent` interpreter args and switch to the
# binary's single-dash `-config`, leaving `agentctl run -config <path>`. This
# keeps the plist single-sourced and Python-shaped on disk.
render_plist() {
    local sed_args
    sed_args=(
        -e "s#__PYTHON__#${PROGRAM}#g"
        -e "s#__CONFIG__#${CONFIG_DST}#g"
        -e "s#__STDOUT__#${LOG_DIR}/agent.out.log#g"
        -e "s#__STDERR__#${LOG_DIR}/agent.err.log#g"
        -e "s#__WORKDIR__#${WORKDIR}#g"
    )
    if [ -n "${GO_BINARY}" ]; then
        sed_args+=(
            -e '\#^[[:space:]]*<string>-m</string>[[:space:]]*$#d'
            -e '\#^[[:space:]]*<string>fallow_agent</string>[[:space:]]*$#d'
            -e 's#<string>--config</string>#<string>-config</string>#'
        )
    fi
    sed "${sed_args[@]}" "${PLIST_TEMPLATE}"
}

if [ "${DRY_RUN}" = "1" ]; then
    render_plist
    exit 0
fi

mkdir -p "${FALLOW_HOME}" "${LOG_DIR}" "${LAUNCH_AGENTS_DIR}"

# ── Install the agent program ────────────────────────────────────────────────
if [ -n "${GO_BINARY}" ]; then
    log "installing Go agent binary -> ${AGENT_BIN}  (untested — verify on target)"
    mkdir -p "${AGENT_BIN_DIR}"
    install -m 0755 "${GO_BINARY}" "${AGENT_BIN}"
else
    # venv via uv (installs the workspace, incl. fallow-agent)
    log "building uv venv in ${FALLOW_REPO}  (untested — verify on target)"
    ( cd "${FALLOW_REPO}" && uv sync --no-dev )
    [ -x "${PROGRAM}" ] || die "expected venv python at ${PROGRAM} after 'uv sync'"
fi

# ── config: copy the example on first install, never clobber a live one ──────
if [ -f "${CONFIG_DST}" ]; then
    log "keeping existing config ${CONFIG_DST}"
elif [ -f "${CONFIG_SRC}" ]; then
    cp "${CONFIG_SRC}" "${CONFIG_DST}"
    log "copied example config -> ${CONFIG_DST} (EDIT IT: enrollment token, coordinator URL, tailnet bind_host, llama_binary path)"
else
    log "WARNING: no config at ${CONFIG_DST} and no example at ${CONFIG_SRC}; create ${CONFIG_DST} before the agent will start"
fi

log "writing LaunchAgent ${PLIST_DST}"
render_plist > "${PLIST_DST}"

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
