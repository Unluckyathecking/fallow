#!/bin/sh
# bootstrap.sh — turn a fresh Mac into an enrolled Fallow agent in one command.
#
# This is a thin orchestrator, not a second installer. It reads the machine
# (OS, CPU arch, RAM, GPU), picks the llama.cpp backend, then hands off to the
# hardened per-OS installer (deploy/macos/install.sh) with the matching flags.
# All the real work — venv build, SHA256 verification against the signed
# manifest, LaunchAgent wiring — stays in install.sh. Nothing is duplicated or
# relaxed here.
#
# macOS only. Windows uses deploy/bootstrap.ps1; the two are siblings, not one
# cross-platform script, because the service managers (launchd vs Task
# Scheduler) share no plumbing worth abstracting.
#
# Enrollment token: pass --token <t> or set FALLOW_ENROLLMENT_TOKEN. The token
# is held in memory only. It is fed to the agent's first run through the launchd
# session environment (launchctl setenv), never written to a file, and cleared
# again once the agent has registered. The agent persists its identity, not the
# token, so nothing secret survives on disk (see ADR 062).
#
# Backend: Apple Silicon -> Metal, Intel -> CPU. There is no CUDA on macOS. The
# choice is passed to install.sh via FALLOW_INSTALL_BACKEND, which already knows
# how to wire each build.
#
# Dry run: --dry-run reports the detection result and delegates to install.sh's
# own FALLOW_INSTALL_DRY_RUN preview. It touches nothing — no uv, no launchctl,
# no enrollment, no self-test. This is the path the acceptance harness drives.
#
# HONESTY: authored in a sandbox. The install, enrollment, and self-test steps
# reach launchd and the network and are marked (untested — verify on target).
set -eu

log()  { printf '[bootstrap] %s\n' "$*" >&2; }
warn() { printf '[bootstrap] WARNING: %s\n' "$*" >&2; }
die()  { printf '[bootstrap] ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
    cat >&2 <<'EOF'
usage: bootstrap.sh [--token <t>] [--go-binary <path>] [--dry-run] [<repo>]

  --token <t>        one-time enrollment token (or set FALLOW_ENROLLMENT_TOKEN)
  --go-binary <path> install a prebuilt agentctl instead of the Python venv
  --dry-run          report detection + delegation, change nothing
  <repo>             fallow checkout for the Python flavour (defaults to this one)
EOF
}

# ── Parse arguments ──────────────────────────────────────────────────────────
TOKEN="${FALLOW_ENROLLMENT_TOKEN:-}"
GO_BINARY=""
REPO=""
DRY_RUN=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --token)     [ "$#" -ge 2 ] || die "--token requires a value"; TOKEN="$2"; shift 2 ;;
        --token=*)   TOKEN="${1#*=}"; shift ;;
        --go-binary) [ "$#" -ge 2 ] || die "--go-binary requires a path"; GO_BINARY="$2"; shift 2 ;;
        --go-binary=*) GO_BINARY="${1#*=}"; shift ;;
        --dry-run)   DRY_RUN=1; shift ;;
        -h|--help)   usage; exit 0 ;;
        --)          shift; break ;;
        -*)          usage; die "unknown option: $1" ;;
        *)           [ -z "${REPO}" ] || die "unexpected argument: $1"; REPO="$1"; shift ;;
    esac
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEPLOY_DIR="${SCRIPT_DIR}"
INSTALLER="${DEPLOY_DIR}/macos/install.sh"
LABEL="com.fallow.agent"
STATE_FILE="${HOME}/.fallow/agent-state.json"
CONFIG_FILE="${HOME}/.fallow/agent.toml"

[ "$(uname -s)" = "Darwin" ] || die "bootstrap.sh is macOS-only; on Windows run deploy\\bootstrap.ps1"
[ -f "${INSTALLER}" ] || die "missing installer ${INSTALLER}"

# ── Detect the machine ───────────────────────────────────────────────────────
ARCH="$(uname -m)"
MEM_BYTES="$(sysctl -n hw.memsize 2>/dev/null || echo 0)"
RAM_GB="$(( MEM_BYTES / 1024 / 1024 / 1024 ))"

case "${ARCH}" in
    arm64) GPU="Apple Silicon"; BACKEND="metal" ;;
    *)     GPU="none";          BACKEND="cpu"   ;;
esac

log "os=macOS arch=${ARCH} ram=${RAM_GB}GB gpu=${GPU} -> backend=${BACKEND}"
[ "${RAM_GB}" -ge 8 ] || warn "only ${RAM_GB}GB RAM; the agent may be starved on a shared machine"

# ── Delegation ───────────────────────────────────────────────────────────────
# Build the install.sh argument list once. Go flavour takes --go-binary; Python
# flavour takes the checkout path (empty = let install.sh default to its own).
set --
if [ -n "${GO_BINARY}" ]; then
    set -- --go-binary "${GO_BINARY}"
elif [ -n "${REPO}" ]; then
    set -- "${REPO}"
fi

if [ "${DRY_RUN}" = "1" ]; then
    log "dry run: delegating to install.sh preview (no side effects)"
    FALLOW_INSTALL_DRY_RUN=1 FALLOW_INSTALL_BACKEND="${BACKEND}" bash "${INSTALLER}" "$@" >/dev/null
    log "dry run OK: detection and delegation exercised, nothing changed"
    exit 0
fi

log "installing (backend=${BACKEND}); install.sh verifies every binary before it runs"
FALLOW_INSTALL_BACKEND="${BACKEND}" bash "${INSTALLER}" "$@"

# ── Enrollment ───────────────────────────────────────────────────────────────
# Feed the one-time token to the agent's first run through the launchd session
# environment, which is in-memory and inherited by the LaunchAgent on restart.
# Wait for the agent to persist its identity, then clear the token. install.sh
# already started the agent once (without a token); kickstart -k restarts it so
# it picks up the token and registers.
wait_for_identity() {
    i=0
    while [ "${i}" -lt 60 ]; do
        [ -f "${STATE_FILE}" ] && return 0
        i=$(( i + 1 ))
        sleep 1
    done
    return 1
}

if [ -n "${TOKEN}" ]; then
    if [ -f "${STATE_FILE}" ]; then
        log "already enrolled (${STATE_FILE} exists); ignoring the supplied token"
    else
        DOMAIN="gui/$(id -u)"
        log "enrolling via one-time token (kept in memory, never written to disk)"
        launchctl setenv FALLOW_ENROLLMENT_TOKEN "${TOKEN}"
        launchctl kickstart -k "${DOMAIN}/${LABEL}" 2>/dev/null || true
        if wait_for_identity; then
            log "enrolled: identity persisted at ${STATE_FILE}"
        else
            warn "no identity after 60s; check ${HOME}/.fallow/logs/agent.err.log"
        fi
        launchctl unsetenv FALLOW_ENROLLMENT_TOKEN
    fi
    # Belt and braces: the token must never have landed in the config file.
    if [ -f "${CONFIG_FILE}" ] && grep -qF "${TOKEN}" "${CONFIG_FILE}"; then
        die "enrollment token found in ${CONFIG_FILE}; refusing to leave a secret on disk"
    fi
else
    log "no enrollment token given; the agent will not register until one is supplied"
fi

# ── Self-test ────────────────────────────────────────────────────────────────
# Report observable post-install state without touching the network: the
# LaunchAgent is loaded and the config is in place.
DOMAIN="gui/$(id -u)"
ok=1
if launchctl print "${DOMAIN}/${LABEL}" >/dev/null 2>&1; then
    log "self-test: LaunchAgent ${LABEL} is loaded"
else
    warn "self-test: LaunchAgent ${LABEL} is not loaded"; ok=0
fi
if [ -f "${CONFIG_FILE}" ]; then
    log "self-test: config present at ${CONFIG_FILE}"
else
    warn "self-test: no config at ${CONFIG_FILE}"; ok=0
fi

[ "${ok}" = "1" ] || die "self-test failed; see warnings above"
log "self-test passed; status: launchctl print ${DOMAIN}/${LABEL}"
