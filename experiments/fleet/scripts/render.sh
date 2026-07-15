#!/bin/sh

set -eu
SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
# shellcheck disable=SC1091
. "$SCRIPT_DIR/lib.sh"
FLEET_DIR=$(CDPATH='' cd -- "$SCRIPT_DIR/.." && pwd)

output=
coordinator_url=
bind_host=
repo=/opt/fallow
llama_binary=/usr/local/bin/llama-server
run_user=fallow

usage() {
    printf '%s\n' "usage: render.sh --output DIR --coordinator-url URL --bind-host ADDRESS [--repo PATH] [--llama-binary PATH] [--run-user USER]"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --output) [ "$#" -ge 2 ] || fail "--output needs a value"; output=$2; shift 2 ;;
        --coordinator-url) [ "$#" -ge 2 ] || fail "--coordinator-url needs a value"; coordinator_url=$2; shift 2 ;;
        --bind-host) [ "$#" -ge 2 ] || fail "--bind-host needs a value"; bind_host=$2; shift 2 ;;
        --repo) [ "$#" -ge 2 ] || fail "--repo needs a value"; repo=$2; shift 2 ;;
        --llama-binary) [ "$#" -ge 2 ] || fail "--llama-binary needs a value"; llama_binary=$2; shift 2 ;;
        --run-user) [ "$#" -ge 2 ] || fail "--run-user needs a value"; run_user=$2; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) fail "unknown option: $1" ;;
    esac
done

require_value output "$output"
require_value coordinator-url "$coordinator_url"
require_value bind-host "$bind_host"
require_value repo "$repo"
require_value llama-binary "$llama_binary"
require_value run-user "$run_user"

case "$coordinator_url" in http://*|https://*) ;; *) fail "coordinator URL must use http or https" ;; esac
case "$coordinator_url" in *[!a-zA-Z0-9:/._-]*) fail "coordinator URL contains unsupported characters" ;; esac
case "$bind_host" in *[!a-zA-Z0-9:._-]*) fail "bind address contains unsupported characters" ;; esac
case "$bind_host" in 0.0.0.0|::) fail "bind address must name the tailnet interface" ;; esac
case "$repo:$llama_binary" in /*:/*) ;; *) fail "repo and llama binary must be absolute paths" ;; esac
case "$repo$llama_binary" in *[!a-zA-Z0-9/._-]*) fail "repo or llama binary path contains unsupported characters" ;; esac
case "$run_user" in [a-zA-Z_]* ) ;; *) fail "run user must start with a letter or _" ;; esac
case "$run_user" in *[!a-zA-Z0-9_-]*|'') fail "run user contains unsupported characters" ;; esac

[ ! -e "$output" ] || fail "output already exists: $output"
mkdir -p "$output"

render() {
    source_file=$1
    target_file=$2
    sed \
        -e "s|@@COORDINATOR_URL@@|$coordinator_url|g" \
        -e "s|@@BIND_HOST@@|$bind_host|g" \
        -e "s|@@FALLOW_REPO@@|$repo|g" \
        -e "s|@@LLAMA_BINARY@@|$llama_binary|g" \
        -e "s|@@RUN_USER@@|$run_user|g" \
        "$source_file" > "$target_file"
}

render "$FLEET_DIR/templates/cloud-init.yaml.tmpl" "$output/cloud-init.yaml"
render "$FLEET_DIR/templates/agent.toml.tmpl" "$output/agent.toml"
render "$FLEET_DIR/templates/fallow-agent.service.tmpl" "$output/fallow-agent.service"
cp "$SCRIPT_DIR/setup.sh" "$SCRIPT_DIR/stop.sh" "$SCRIPT_DIR/cleanup.sh" "$SCRIPT_DIR/lib.sh" "$output/"
chmod 0755 "$output/setup.sh" "$output/stop.sh" "$output/cleanup.sh"
chmod 0644 "$output/cloud-init.yaml" "$output/agent.toml" "$output/fallow-agent.service" "$output/lib.sh"

"$SCRIPT_DIR/validate.sh" "$output"
printf '%s\n' "rendered fleet bundle at $output"
