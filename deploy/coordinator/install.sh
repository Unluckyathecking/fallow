#!/usr/bin/env bash
# install.sh — install the Fallow coordinator as a systemd service on Linux.
#
# One command on the machine that stays up:
#
#   sudo install.sh --ref v0.3.0
#
# It creates a `fallow` system user, checks the repository out at that ref under
# /opt/fallow/src, builds the venv with `uv sync --frozen`, puts the config in
# /etc/fallow and the state in /var/lib/fallow, installs
# fallow-coordinator.service, and starts it. Re-running it with a newer --ref is
# the upgrade path: fetch, check out, re-sync, restart.
#
# The unit and the example config are taken from the checkout, not from beside
# this script, so the service definition always matches the ref that is
# deployed and this file can be curled on its own from a release tag.
#
# Prerequisites, both checked before anything is written:
#   - root (this installs a system service, a system user and system paths)
#   - git and uv (https://docs.astral.sh/uv/). The script does NOT install uv:
#     CI pins it through astral-sh/setup-uv, and piping an unpinned installer
#     into a shell on a school server is not a trade this house makes. Install
#     uv the way your distribution or that action does, then re-run.
#
# --dry-run prints the plan and touches nothing (and needs no root), the same
# preview seam deploy/bootstrap.sh and deploy/macos/install.sh carry.
#
# Linux only. macOS coordinators keep the launchd pattern of the agent plist.
#
# HONESTY: authored in a sandbox with no systemd PID 1. Argument handling, the
# plan, and the unit file itself are tested (tests/deploy/test_coordinator_install.py,
# `systemd-analyze verify`); the clone, the venv build and the systemctl calls
# are marked (untested — verify on target).
set -euo pipefail

REPO_URL="https://github.com/Unluckyathecking/fallow.git"
SERVICE_USER="fallow"
SRC_DIR="/opt/fallow/src"
STATE_DIR="/var/lib/fallow"
CONFIG_DIR="/etc/fallow"
CONFIG_DST="${CONFIG_DIR}/coordinator.toml"
CONFIG_SRC="${SRC_DIR}/deploy/coordinator.example.toml"
UNIT_NAME="fallow-coordinator.service"
UNIT_SRC="${SRC_DIR}/deploy/coordinator/${UNIT_NAME}"
UNIT_DST="/etc/systemd/system/${UNIT_NAME}"

log()  { printf '[install] %s\n' "$*" >&2; }
die()  { printf '[install] ERROR: %s\n' "$*" >&2; exit 1; }
plan() { printf 'plan: %s\n' "$*"; }

usage() {
    cat >&2 <<'EOF'
usage: install.sh [install] --ref <vX.Y.Z> [--allow-branch] [--no-start] [--dry-run]
       install.sh uninstall [--purge] [--dry-run]

  --ref <vX.Y.Z>  the git ref to deploy; must be a release tag
  --allow-branch  accept a ref that is not a vX.Y.Z release tag
  --no-start      install the unit, do not enable or start it
  --dry-run       print the plan, change nothing (needs no root)
  --purge         uninstall: also delete /etc/fallow and /var/lib/fallow
EOF
}

# run <command...> — execute it, or print it as a plan line under --dry-run.
run() {
    if [ "${DRY_RUN}" -eq 1 ]; then
        plan "$*"
    else
        "$@"
    fi
}

# ── Parse arguments ──────────────────────────────────────────────────────────
VERB="install"
case "${1:-}" in
    install|uninstall) VERB="$1"; shift ;;
esac

REF=""
ALLOW_BRANCH=0
NO_START=0
DRY_RUN=0
PURGE=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --ref)          [ "$#" -ge 2 ] || die "--ref requires a value"; REF="$2"; shift 2 ;;
        --ref=*)        REF="${1#*=}"; shift ;;
        --allow-branch) ALLOW_BRANCH=1; shift ;;
        --no-start)     NO_START=1; shift ;;
        --dry-run)      DRY_RUN=1; shift ;;
        --purge)        PURGE=1; shift ;;
        -h|--help)      usage; exit 0 ;;
        *)              usage; die "unknown option: $1" ;;
    esac
done

# ── Preconditions ────────────────────────────────────────────────────────────
# The preview needs no privilege and no tools: it only prints what the real run
# would check and do, so anyone reviewing the plan can run it.
require_root() {
    if [ "${DRY_RUN}" -eq 1 ]; then
        plan "check: running as root"
        return
    fi
    [ "$(id -u)" -eq 0 ] || die "must run as root: this manages a system service, a system user and /etc + /var/lib paths"
}

require_tools() {
    if [ "${DRY_RUN}" -eq 1 ]; then
        plan "check: git is installed"
        plan "check: uv is installed"
        return
    fi
    command -v git >/dev/null || die "git is required"
    command -v uv >/dev/null || die "uv is required (https://docs.astral.sh/uv/); install it first, this script will not"
}

do_install() {
    [ -n "${REF}" ] || die "--ref <vX.Y.Z> is required; a pilot deploys a pinned release tag (docs/releasing.md)"
    [ "${PURGE}" -eq 0 ] || die "--purge is an uninstall option"
    # A branch moves under the machine. Pilots deploy tags; anything else is an
    # explicit, argued-for choice.
    if ! [[ "${REF}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?$ ]] && [ "${ALLOW_BRANCH}" -eq 0 ]; then
        die "refusing an unpinned ref: ${REF} (deploy a vX.Y.Z release tag, or pass --allow-branch)"
    fi

    require_root
    require_tools

    # ── System user ──────────────────────────────────────────────────────────
    # No login shell: nothing should ever log in as the coordinator.
    if id -u "${SERVICE_USER}" >/dev/null 2>&1; then
        log "system user ${SERVICE_USER} already exists"
    else
        nologin="/usr/sbin/nologin"
        [ -x "${nologin}" ] || nologin="/sbin/nologin"
        [ -x "${nologin}" ] || nologin="/bin/false"
        run useradd --system --home-dir "${STATE_DIR}" --shell "${nologin}" "${SERVICE_USER}"
    fi

    # ── Source checkout at the pinned ref ────────────────────────────────────
    # (untested — verify on target: this reaches the network.)
    run install -d -m 0755 "$(dirname "${SRC_DIR}")"
    if [ -d "${SRC_DIR}/.git" ]; then
        log "updating the existing checkout at ${SRC_DIR}"
    else
        run git clone "${REPO_URL}" "${SRC_DIR}"
    fi
    # Fetch the one ref and check out its commit detached: the deployed tree is
    # the ref that was asked for, not a branch that can move under it.
    run git -C "${SRC_DIR}" fetch --tags --prune origin "${REF}"
    run git -C "${SRC_DIR}" checkout --force --detach FETCH_HEAD

    if [ "${DRY_RUN}" -eq 0 ]; then
        [ -f "${UNIT_SRC}" ] || die "no unit file at ${UNIT_SRC}; is ${REF} old enough to predate it?"
        [ -f "${CONFIG_SRC}" ] || die "no example config at ${CONFIG_SRC}"
    fi

    # ── Virtualenv from the lockfile ─────────────────────────────────────────
    # --frozen: install exactly uv.lock, never re-resolve on the school server.
    # --no-dev: the coordinator host has no use for the test and lint toolchain.
    # (untested — verify on target: this reaches the network.)
    run uv sync --frozen --no-dev --project "${SRC_DIR}"

    # ── State and config ─────────────────────────────────────────────────────
    # State is the service user's. /etc/fallow is root's, group-readable by the
    # service, because it holds the admin key and the Site Mode TLS key.
    run install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0750 "${STATE_DIR}"
    run install -d -o root -g "${SERVICE_USER}" -m 0750 "${CONFIG_DIR}"
    if [ -f "${CONFIG_DST}" ]; then
        log "keeping the existing config ${CONFIG_DST}"
    else
        run install -o root -g "${SERVICE_USER}" -m 0640 "${CONFIG_SRC}" "${CONFIG_DST}"
        log "copied the example config -> ${CONFIG_DST}"
        log "EDIT IT before this is useful: admin_key (or set FALLOW_COORD_ADMIN_KEY), host (the exact address to serve on), and under [site] tls_certfile and tls_keyfile for a Site Mode pilot"
        log "a TLS key under ${CONFIG_DIR} must be group-readable by ${SERVICE_USER}, or the service cannot start"
    fi

    # ── Service ──────────────────────────────────────────────────────────────
    # (untested — verify on target: these reach systemd.)
    run install -m 0644 "${UNIT_SRC}" "${UNIT_DST}"
    run systemctl daemon-reload
    if [ "${NO_START}" -eq 1 ]; then
        log "installed ${UNIT_DST}; --no-start given, so start it yourself: systemctl enable --now ${UNIT_NAME}"
    else
        # restart, not start: a re-run with a newer --ref must pick up the new code.
        run systemctl enable "${UNIT_NAME}"
        run systemctl restart "${UNIT_NAME}"
        log "status: systemctl status ${UNIT_NAME}    logs: journalctl -u ${UNIT_NAME} -f"
    fi
}

do_uninstall() {
    [ -z "${REF}" ] || die "--ref is an install option"
    require_root

    # (untested — verify on target: this reaches systemd.)
    run systemctl disable --now "${UNIT_NAME}" || true
    run rm -f "${UNIT_DST}"
    run systemctl daemon-reload
    run rm -rf "${SRC_DIR}"
    if [ "${PURGE}" -eq 1 ]; then
        run rm -rf "${STATE_DIR}" "${CONFIG_DIR}"
        log "purged ${STATE_DIR} and ${CONFIG_DIR}"
    else
        log "preserved ${STATE_DIR} and ${CONFIG_DIR}; re-run with --purge to delete them"
    fi
    log "the ${SERVICE_USER} system user is left in place; remove it by hand if you are done with this host"
}

case "${VERB}" in
    install)   do_install ;;
    uninstall) do_uninstall ;;
esac
