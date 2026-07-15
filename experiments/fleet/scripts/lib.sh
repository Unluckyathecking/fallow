#!/bin/sh

set -eu

fail() {
    printf '%s\n' "fleet scaffold: $*" >&2
    exit 1
}

require_value() {
    name=$1
    value=$2
    newline=$(printf '\n_')
    newline=${newline%_}
    carriage_return=$(printf '\r_')
    carriage_return=${carriage_return%_}
    [ -n "$value" ] || fail "$name must not be empty"
    case "$value" in
        *'@@'*) fail "$name contains a template marker" ;;
        *"$newline"*|*"$carriage_return"*) fail "$name must fit on one line" ;;
    esac
}
