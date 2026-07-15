#!/bin/sh

set -eu
SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
# shellcheck disable=SC1091
. "$SCRIPT_DIR/lib.sh"

[ "$#" -eq 1 ] || fail "usage: setup.sh BUNDLE"
bundle=$1
root=${FALLOW_ROOT:-}
token=${FALLOW_ENROLLMENT_TOKEN:-}
[ -n "$token" ] || fail "FALLOW_ENROLLMENT_TOKEN must be supplied through the host environment"
case "$token" in *[!a-zA-Z0-9._~-]*) fail "FALLOW_ENROLLMENT_TOKEN contains unsupported characters" ;; esac

for name in agent.toml fallow-agent.service; do
    [ -f "$bundle/$name" ] || fail "bundle is missing $name"
done

install -d -m 0750 "$root/etc/fallow" "$root/var/lib/fallow" "$root/var/log/fallow"
install -d -m 0755 "$root/etc/systemd/system"
install -m 0640 "$bundle/agent.toml" "$root/etc/fallow/agent.toml"
install -m 0644 "$bundle/fallow-agent.service" "$root/etc/systemd/system/fallow-agent.service"
umask 077
env_file="$root/etc/fallow/agent.env"
env_tmp="$env_file.tmp.$$"
trap 'rm -f "$env_tmp"' 0 HUP INT TERM
printf 'FALLOW_ENROLLMENT_TOKEN=%s\n' "$token" > "$env_tmp"
chmod 0600 "$env_tmp"
mv -f "$env_tmp" "$env_file"
trap - 0 HUP INT TERM

if [ -n "$root" ]; then
    printf '%s\n' "staged Fallow agent files under $root"
    exit 0
fi

run_user=$(sed -n 's/^User=//p' "$bundle/fallow-agent.service")
[ -n "$run_user" ] || fail "service account is missing from the unit"
id "$run_user" >/dev/null 2>&1 || fail "service account does not exist: $run_user"
chown -R "$run_user:$run_user" /var/lib/fallow /var/log/fallow
systemctl daemon-reload
systemctl enable --now fallow-agent.service
printf '%s\n' "installed and started the Fallow agent"
