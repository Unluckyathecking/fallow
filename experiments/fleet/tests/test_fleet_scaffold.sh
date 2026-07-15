#!/bin/sh

set -eu
TEST_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
FLEET_DIR=$(CDPATH='' cd -- "$TEST_DIR/.." && pwd)
SCRIPTS=$FLEET_DIR/scripts

work=$(mktemp -d "${TMPDIR:-/tmp}/fallow-fleet-test.XXXXXX")
trap 'rm -rf "$work"' EXIT HUP INT TERM

for script in "$SCRIPTS"/*.sh; do
    sh -n "$script"
done

"$SCRIPTS/render.sh" \
    --output "$work/bundle" \
    --coordinator-url http://coord.test.ts.net:8080 \
    --bind-host 100.64.12.34 \
    --repo /srv/fallow \
    --llama-binary /opt/llama/llama-server \
    --run-user fallow-test
"$SCRIPTS/validate.sh" "$work/bundle"

grep -q 'http://coord.test.ts.net:8080' "$work/bundle/agent.toml"
grep -q '100.64.12.34' "$work/bundle/agent.toml"
grep -q 'User=fallow-test' "$work/bundle/fallow-agent.service"
if grep -R -q -E '@@[A-Z0-9_]+@@|tskey-(auth|client)-' "$work/bundle"; then
    exit 1
fi
if grep -R -q -E 'FALLOW_ENROLLMENT_TOKEN=[^%]' "$work/bundle"; then
    exit 1
fi

cp -R "$work/bundle" "$work/unresolved"
printf '%s\n' '@@MISSING_VALUE@@' >> "$work/unresolved/agent.toml"
if "$SCRIPTS/validate.sh" "$work/unresolved" >/dev/null 2>&1; then
    exit 1
fi

cp -R "$work/bundle" "$work/secret"
printf '%s\n' 'token=tskey-auth-examplecredential' >> "$work/secret/agent.toml"
if "$SCRIPTS/validate.sh" "$work/secret" >/dev/null 2>&1; then
    exit 1
fi

cp -R "$work/bundle" "$work/network"
printf '%s\n' 'curl https://example.test' >> "$work/network/setup.sh"
if "$SCRIPTS/validate.sh" "$work/network" >/dev/null 2>&1; then
    exit 1
fi

mkdir "$work/root"
FALLOW_ROOT=$work/root FALLOW_ENROLLMENT_TOKEN=test-one-time-token \
    "$work/bundle/setup.sh" "$work/bundle"
test "$(stat -f '%Lp' "$work/root/etc/fallow/agent.env" 2>/dev/null || stat -c '%a' "$work/root/etc/fallow/agent.env")" = 600
grep -q '^FALLOW_ENROLLMENT_TOKEN=test-one-time-token$' "$work/root/etc/fallow/agent.env"

"$SCRIPTS/dry-run.sh"
printf '%s\n' "fleet scaffold tests passed"
