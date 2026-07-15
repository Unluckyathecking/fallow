#!/usr/bin/env bash
# uninstall.sh — remove the Fallow agent LaunchAgent on macOS.
#
# Removes the LaunchAgent from the user's GUI session and deletes the plist.
# By default it PRESERVES ~/.fallow (config, model cache, logs). Pass --purge to
# delete ~/.fallow as well. It never touches the git checkout or deploy/bin.
set -euo pipefail

log() { printf '[uninstall] %s\n' "$*" >&2; }

LABEL="com.fallow.agent"
PLIST_DST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
FALLOW_HOME="${HOME}/.fallow"
DOMAIN="gui/$(id -u)"

PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

log "unloading ${DOMAIN}/${LABEL}  (untested — verify on target)"
launchctl bootout "${DOMAIN}/${LABEL}" 2>/dev/null || true

if [ -f "${PLIST_DST}" ]; then
    rm -f "${PLIST_DST}"
    log "removed ${PLIST_DST}"
fi

if [ "${PURGE}" -eq 1 ]; then
    rm -rf "${FALLOW_HOME}"
    log "purged ${FALLOW_HOME}"
else
    log "preserved ${FALLOW_HOME} (config, models, logs); re-run with --purge to delete it"
fi

log "done"
