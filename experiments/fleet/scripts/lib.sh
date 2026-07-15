#!/bin/sh

set -eu

fail() {
    printf '%s\n' "fleet scaffold: $*" >&2
    exit 1
}

require_value() {
    name=$1
    value=$2
    [ -n "$value" ] || fail "$name must not be empty"
    case "$value" in
        *'@@'*) fail "$name contains a template marker" ;;
        *'\n'*|*'\r'*) fail "$name must fit on one line" ;;
    esac
}
