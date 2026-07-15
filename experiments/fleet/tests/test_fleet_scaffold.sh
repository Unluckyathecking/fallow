#!/bin/sh

set -eu
TEST_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
FLEET_DIR=$(CDPATH='' cd -- "$TEST_DIR/.." && pwd)
SCRIPTS=$FLEET_DIR/scripts

fail() {
    printf '%s\n' "fleet scaffold test failed: $*" >&2
    exit 1
}

run_step() {
    label=$1
    shift
    printf '%s\n' "fleet scaffold test: $label"
    "$@" || fail "$label"
}

expect_rejected() {
    label=$1
    bundle=$2
    log=$3
    if "$SCRIPTS/validate.sh" "$bundle" > "$log" 2>&1; then
        cat "$log" >&2
        fail "$label bundle was accepted"
    fi
}

work=$(mktemp -d "${TMPDIR:-/tmp}/fallow-fleet-test.XXXXXX")
trap 'rm -rf "$work"' EXIT HUP INT TERM

for script in "$SCRIPTS"/*.sh; do
    run_step "syntax check for $(basename "$script")" sh -n "$script"
done

run_step "render public bundle" "$SCRIPTS/render.sh" \
    --output "$work/bundle" \
    --coordinator-url http://coord.test.ts.net:8080 \
    --bind-host 100.64.12.34 \
    --repo /srv/fallow \
    --llama-binary /opt/llama/llama-server \
    --run-user fallow-test
run_step "validate public bundle" "$SCRIPTS/validate.sh" "$work/bundle"

grep -q 'http://coord.test.ts.net:8080' "$work/bundle/agent.toml" || fail "coordinator URL was not rendered"
grep -q '100.64.12.34' "$work/bundle/agent.toml" || fail "bind address was not rendered"
grep -q 'User=fallow-test' "$work/bundle/fallow-agent.service" || fail "service account was not rendered"
if grep -R -q -E '@@[A-Z0-9_]+@@|tskey-(auth|client)-' "$work/bundle"; then
    grep -R -n -E '@@[A-Z0-9_]+@@|tskey-(auth|client)-' "$work/bundle" >&2
    fail "public bundle contains a marker or credential"
fi
if grep -R -q -E 'FALLOW_ENROLLMENT_TOKEN=[^%]' "$work/bundle"; then
    grep -R -n -E 'FALLOW_ENROLLMENT_TOKEN=[^%]' "$work/bundle" >&2
    fail "public bundle contains an enrollment token"
fi

cp -R "$work/bundle" "$work/unresolved"
printf '%s\n' '@@MISSING_VALUE@@' >> "$work/unresolved/agent.toml"
expect_rejected "unresolved marker" "$work/unresolved" "$work/unresolved.log"

cp -R "$work/bundle" "$work/secret"
printf '%s\n' 'token=tskey-auth-examplecredential' >> "$work/secret/agent.toml"
expect_rejected "embedded secret" "$work/secret" "$work/secret.log"

cp -R "$work/bundle" "$work/network"
printf '%s\n' 'curl https://example.test' >> "$work/network/setup.sh"
expect_rejected "network command" "$work/network" "$work/network.log"

mkdir "$work/root"
run_step "stage admitted host files" env \
    FALLOW_ROOT="$work/root" \
    FALLOW_ENROLLMENT_TOKEN=test-one-time-token \
    "$work/bundle/setup.sh" "$work/bundle"
# GNU stat accepts -f as filesystem mode instead of rejecting BSD syntax. Try GNU -c first.
mode=$(stat -c '%a' "$work/root/etc/fallow/agent.env" 2>/dev/null || stat -f '%Lp' "$work/root/etc/fallow/agent.env")
[ "$mode" = 600 ] || fail "runtime credential mode is $mode, expected 600"
grep -q '^FALLOW_ENROLLMENT_TOKEN=test-one-time-token$' "$work/root/etc/fallow/agent.env" || fail "runtime credential was not staged"

run_step "sealed offline dry run" "$SCRIPTS/dry-run.sh"
printf '%s\n' "fleet scaffold tests passed"
