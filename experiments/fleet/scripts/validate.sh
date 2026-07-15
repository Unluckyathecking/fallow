#!/bin/sh

set -eu
SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
# shellcheck disable=SC1091
. "$SCRIPT_DIR/lib.sh"

[ "$#" -eq 1 ] || fail "usage: validate.sh BUNDLE"
bundle=$1
[ -d "$bundle" ] || fail "bundle does not exist: $bundle"

for name in cloud-init.yaml agent.toml fallow-agent.service setup.sh stop.sh cleanup.sh lib.sh; do
    [ -f "$bundle/$name" ] || fail "bundle is missing $name"
done

if grep -R -n -E '@@[A-Z0-9_]+@@' "$bundle"; then
    fail "bundle has unresolved template markers"
fi
if grep -R -n -E 'tskey-(auth|client)-|Bearer[[:space:]]+[A-Za-z0-9._~-]{12,}|enrollment_token[[:space:]]*=[[:space:]]*"[^$]' "$bundle"; then
    fail "bundle appears to contain a credential"
fi
if grep -R -n -E '(^|[[:space:]])(curl|wget|ssh|scp|rsync[[:space:]].*:|aws|az|gcloud|doctl|linode-cli)([[:space:]]|$)' "$bundle"; then
    fail "bundle contains a network or cloud command"
fi
grep -q '^#cloud-config$' "$bundle/cloud-init.yaml" || fail "cloud-init header is missing"
grep -q '^EnvironmentFile=/etc/fallow/agent.env$' "$bundle/fallow-agent.service" || fail "service has no runtime credential file"
grep -q '^bind_host = "' "$bundle/agent.toml" || fail "agent bind address is missing"
if grep -q '^bind_host = "\(0\.0\.0\.0\|::\)"$' "$bundle/agent.toml"; then
    fail "agent config binds every interface"
fi

for script in "$bundle/setup.sh" "$bundle/stop.sh" "$bundle/cleanup.sh" "$bundle/lib.sh"; do
    sh -n "$script"
done
printf '%s\n' "validated fleet bundle at $bundle"
