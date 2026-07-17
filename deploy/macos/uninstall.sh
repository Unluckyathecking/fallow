#!/usr/bin/env bash
# uninstall.sh — remove the Fallow agent LaunchAgent on macOS.
#
# Boots the LaunchAgent out of the user's GUI session, deletes the plist, stops
# any agent process the bootout left behind, and frees the replica ports it may
# have bound. By default it PRESERVES ~/.fallow (config, model cache, logs); pass
# --purge to delete ~/.fallow too. It never touches the git checkout or deploy/bin.
set -euo pipefail

log() { printf '[uninstall] %s\n' "$*" >&2; }

LABEL="com.fallow.agent"
PLIST_DST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
FALLOW_HOME="${HOME}/.fallow"
CONFIG="${FALLOW_HOME}/agent.toml"
DOMAIN="gui/$(id -u)"

PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

log "unloading ${DOMAIN}/${LABEL}  (untested — verify on target)"
launchctl bootout "${DOMAIN}/${LABEL}" 2>/dev/null || true

if [ -f "${PLIST_DST}" ]; then
    rm -f "${PLIST_DST}"
    log "removed ${PLIST_DST}"
fi

# ── Free the replica ports ───────────────────────────────────────────────────
# The agent spawns llama-server replicas across a contiguous port range; a
# bootout should stop them, but a crashed agent can orphan one. Read the range
# from the installed config (env override wins), fall back to the shipped
# default, and terminate whatever still owns each port. Best-effort: no lsof, or
# nothing bound, is a no-op.
port_start="${FALLOW_PORT_START:-}"
port_count="${FALLOW_PORT_COUNT:-}"
if [ -f "${CONFIG}" ]; then
    [ -n "${port_start}" ] || port_start="$(awk -F= '/^[[:space:]]*start[[:space:]]*=/{gsub(/[^0-9]/,"",$2);print $2;exit}' "${CONFIG}")"
    [ -n "${port_count}" ] || port_count="$(awk -F= '/^[[:space:]]*count[[:space:]]*=/{gsub(/[^0-9]/,"",$2);print $2;exit}' "${CONFIG}")"
fi
port_start="${port_start:-8100}"
port_count="${port_count:-16}"

if command -v lsof >/dev/null 2>&1; then
    freed=0
    port="${port_start}"
    end=$(( port_start + port_count ))
    while [ "${port}" -lt "${end}" ]; do
        pids="$(lsof -ti "tcp:${port}" -sTCP:LISTEN 2>/dev/null || true)"
        if [ -n "${pids}" ]; then
            # shellcheck disable=SC2086
            kill ${pids} 2>/dev/null || true
            freed=$(( freed + 1 ))
        fi
        port=$(( port + 1 ))
    done
    [ "${freed}" -eq 0 ] || log "freed ${freed} replica port(s) in ${port_start}..$(( end - 1 ))"
else
    log "lsof not found; skipping replica-port cleanup"
fi

if [ "${PURGE}" -eq 1 ]; then
    rm -rf "${FALLOW_HOME}"
    log "purged ${FALLOW_HOME}"
else
    log "preserved ${FALLOW_HOME} (config, models, logs); re-run with --purge to delete it"
fi

log "done"
