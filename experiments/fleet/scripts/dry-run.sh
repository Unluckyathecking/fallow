#!/bin/sh

set -eu
SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
# shellcheck disable=SC1091
. "$SCRIPT_DIR/lib.sh"

work=$(mktemp -d "${TMPDIR:-/tmp}/fallow-fleet-dry-run.XXXXXX")
trap 'rm -rf "$work"' EXIT HUP INT TERM
mkdir "$work/bin"
: > "$work/network.log"
export FALLOW_NETWORK_LOG="$work/network.log"

for command in curl wget ssh scp rsync aws az gcloud doctl linode-cli tailscale; do
    stub="$work/bin/$command"
    # The quoted variables belong to the generated stub, not this process.
    # shellcheck disable=SC2016
    printf '%s\n' '#!/bin/sh' 'printf "%s\n" "$0" >> "$FALLOW_NETWORK_LOG"' 'printf "%s\n" "network command blocked during dry run" >&2' 'exit 97' > "$stub"
    chmod 0755 "$stub"
done

PATH="$work/bin:/usr/bin:/bin" "$SCRIPT_DIR/render.sh" \
    --output "$work/bundle" \
    --coordinator-url http://coordinator.example.ts.net:8080 \
    --bind-host 100.64.0.20 \
    --repo /opt/fallow \
    --llama-binary /usr/local/bin/llama-server \
    --run-user fallow
PATH="$work/bin:/usr/bin:/bin" "$SCRIPT_DIR/validate.sh" "$work/bundle"
[ ! -s "$work/network.log" ] || fail "dry run attempted a network command"
printf '%s\n' "fleet dry run passed without network access"
