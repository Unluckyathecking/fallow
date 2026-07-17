#!/bin/sh
# verify-sha256.sh — check one file's SHA256 against a signed manifest.
#
# Usage: verify-sha256.sh <file> <manifest>
#
# The manifest is `shasum -a 256` format: one `<hex>  <path>` line per file.
# Verification matches on basename, so a manifest that ships release-relative
# paths still verifies a binary sitting anywhere on the target. It fails closed:
# a missing file, a missing manifest, or a basename with no manifest entry all
# exit non-zero. Nothing runs the binary until this returns 0.
#
# Split out from install.sh so the check is one job with one responsibility and
# can be exercised on its own by render_test.sh with throwaway fixtures.
set -eu

die() { printf '[verify-sha256] ERROR: %s\n' "$*" >&2; exit 1; }

[ "$#" -eq 2 ] || die "usage: verify-sha256.sh <file> <manifest>"
file="$1"
manifest="$2"

[ -f "${file}" ]     || die "no file at ${file}"
[ -f "${manifest}" ] || die "no manifest at ${manifest}"
command -v shasum >/dev/null || die "shasum is required"

base="$(basename "${file}")"

# Expected hash: first manifest line whose path column has this basename.
expected="$(awk -v b="${base}" '
    { n = split($2, parts, "/"); if (parts[n] == b) { print $1; exit } }
' "${manifest}")"
[ -n "${expected}" ] || die "no signed hash for ${base} in ${manifest}"

actual="$(shasum -a 256 "${file}" | awk '{print $1}')"
[ "${expected}" = "${actual}" ] \
    || die "sha256 mismatch for ${base}: manifest ${expected} != file ${actual}"

printf '[verify-sha256] OK: %s matches manifest\n' "${base}" >&2
