#!/bin/sh

set -eu
SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
# shellcheck disable=SC1091
. "$SCRIPT_DIR/lib.sh"

[ "$#" -eq 0 ] || fail "usage: stop.sh"
root=${FALLOW_ROOT:-}
if [ -n "$root" ]; then
    printf '%s\n' "staged roots do not run systemd services"
    exit 0
fi
systemctl stop fallow-agent.service
printf '%s\n' "stopped the Fallow agent"
