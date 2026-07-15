#!/bin/sh

set -eu
SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
# shellcheck disable=SC1091
. "$SCRIPT_DIR/lib.sh"

purge=false
case "${1:-}" in
    '') ;;
    --purge) purge=true ;;
    *) fail "usage: cleanup.sh [--purge]" ;;
esac

root=${FALLOW_ROOT:-}
if [ -z "$root" ]; then
    systemctl disable --now fallow-agent.service 2>/dev/null || true
fi
rm -f "$root/etc/systemd/system/fallow-agent.service"
rm -rf "$root/etc/fallow"
if [ "$purge" = true ]; then
    rm -rf "$root/var/lib/fallow" "$root/var/log/fallow"
fi
if [ -z "$root" ]; then
    systemctl daemon-reload
fi
printf '%s\n' "removed Fallow agent service files"
