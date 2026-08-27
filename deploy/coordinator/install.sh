#!/usr/bin/env bash
# install.sh — install the Fallow coordinator as a systemd service on Linux.
#
# One command on the machine that stays up:
#
#   sudo install.sh --ref v0.3.0
#
# It creates a `fallow` system user, checks the repository out at that ref under
# /opt/fallow/src, builds the venv with `uv sync --frozen`, puts the config in
# /etc/fallow and the state in /var/lib/fallow, installs
# fallow-coordinator.service, and starts it. Re-running it with a newer --ref is
# the upgrade path: stop the running service, fetch, check out, re-sync, start.
# The stop is not optional: the venv imports the code from /opt/fallow/src, so
# rewriting that tree under a live process is rewriting the program it is running.
#
# A run that would start the coordinator with the example's published
# placeholder admin key installs the unit without starting it. That covers the
# first run, which seeds /etc/fallow/coordinator.toml from the example, and any
# later run whose config still carries the placeholder — editing host and the TLS
# paths and leaving admin_key alone is the easy mistake, and file presence never
# caught it. Set admin_key, or FALLOW_COORD_ADMIN_KEY in
# /etc/fallow/coordinator.env, then re-run.
#
# The unit and the example config are taken from the checkout, not from beside
# this script, so the service definition always matches the ref that is
# deployed and this file can be curled on its own from a release tag.
#
# Prerequisites, both checked before anything is written:
#   - root (this installs a system service, a system user and system paths)
#   - git and uv (https://docs.astral.sh/uv/). The script does NOT install uv:
#     CI pins it through astral-sh/setup-uv, and piping an unpinned installer
#     into a shell on a school server is not a trade this house makes. Install
#     uv the way your distribution or that action does, then re-run.
#   - egress to github.com and PyPI. `uv sync` resolves nothing new, but it does
#     download the wheels it has not cached AND a managed CPython 3.12 (this
#     workspace pins python-preference = "only-managed"), so a zero-egress lab
#     cannot use this path — see deploy/OFFLINE.md for the offline bundle. That
#     interpreter is installed under /opt/fallow/python (UV_PYTHON_INSTALL_DIR):
#     uv's default is the invoking user's private data dir, which on a root
#     install sits under a 0700 /root the fallow service can never traverse.
#
# --dry-run prints the plan and touches nothing (and needs no root), the same
# preview seam deploy/bootstrap.sh and deploy/macos/install.sh carry.
#
# Linux only. macOS coordinators keep the launchd pattern of the agent plist.
#
# HONESTY: authored in a sandbox with no systemd PID 1. Argument handling, the
# plan, and the unit file itself are tested (tests/deploy/test_coordinator_install.py,
# `systemd-analyze verify`); the clone, the venv build and the systemctl calls
# are marked (untested — verify on target).
set -euo pipefail

REPO_URL="https://github.com/Unluckyathecking/fallow.git"
SERVICE_USER="fallow"
# Every system path hangs off one prefix. It is empty in every real run; the
# tests set FALLOW_INSTALL_ROOT to a temporary directory, which is the only way
# to exercise the config and unit-present branches on a host with no systemd.
PREFIX="${FALLOW_INSTALL_ROOT:-}"
SRC_DIR="${PREFIX}/opt/fallow/src"
# Where uv installs the managed CPython the venv links against. Left to its
# default it lands under the invoking user's private data dir — /root/.local
# on a root install, behind a 0700 /root — and .venv/bin/python is a symlink
# into it, so the fallow service could never reach its own interpreter.
PYTHON_DIR="${PREFIX}/opt/fallow/python"
STATE_DIR="${PREFIX}/var/lib/fallow"
CONFIG_DIR="${PREFIX}/etc/fallow"
CONFIG_DST="${CONFIG_DIR}/coordinator.toml"
CONFIG_SRC="${SRC_DIR}/deploy/coordinator.example.toml"
ENV_DST="${CONFIG_DIR}/coordinator.env"
# The admin key the example config ships. A service started with this is a
# coordinator whose admin key is published in this repository.
PLACEHOLDER_ADMIN_KEY="change-me-to-a-long-random-string"
UNIT_NAME="fallow-coordinator.service"
UNIT_SRC="${SRC_DIR}/deploy/coordinator/${UNIT_NAME}"
UNIT_DST="${PREFIX}/etc/systemd/system/${UNIT_NAME}"

log()  { printf '[install] %s\n' "$*" >&2; }
die()  { printf '[install] ERROR: %s\n' "$*" >&2; exit 1; }
plan() { printf 'plan: %s\n' "$*"; }

usage() {
    cat >&2 <<'EOF'
usage: install.sh [install] --ref <vX.Y.Z> [--allow-branch] [--no-start] [--dry-run]
       install.sh uninstall [--purge] [--dry-run]

  --ref <vX.Y.Z>  the git ref to deploy; must be a release tag
  --allow-branch  accept a ref that is not a vX.Y.Z release tag
  --no-start      install the unit, do not enable or start it
  --dry-run       print the plan, change nothing (needs no root)
  --purge         uninstall: also delete /etc/fallow and /var/lib/fallow
  --allow-external-standby
                  accept a standby_path outside /var/lib/fallow; only correct
                  once you have widened the unit's ReadWritePaths by hand
EOF
}

# run <command...> — execute it, or print it as a plan line under --dry-run.
run() {
    if [ "${DRY_RUN}" -eq 1 ]; then
        plan "$*"
    else
        "$@"
    fi
}

# ── Parse arguments ──────────────────────────────────────────────────────────
VERB="install"
case "${1:-}" in
    install|uninstall) VERB="$1"; shift ;;
esac

REF=""
ALLOW_BRANCH=0
NO_START=0
DRY_RUN=0
PURGE=0
ALLOW_EXTERNAL_STANDBY=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        # A ref that looks like an option is a swallowed flag, not a ref:
        # `--ref --dry-run` must not quietly become a real run of "--dry-run".
        --ref)          [ "$#" -ge 2 ] || die "--ref requires a value"
                        case "$2" in -*) die "--ref requires a value, got the option $2" ;; esac
                        REF="$2"; shift 2 ;;
        --ref=*)        REF="${1#*=}"; shift ;;
        --allow-branch) ALLOW_BRANCH=1; shift ;;
        --no-start)     NO_START=1; shift ;;
        --dry-run)      DRY_RUN=1; shift ;;
        --purge)        PURGE=1; shift ;;
        --allow-external-standby) ALLOW_EXTERNAL_STANDBY=1; shift ;;
        -h|--help)      usage; exit 0 ;;
        *)              usage; die "unknown option: $1" ;;
    esac
done

# ── Preconditions ────────────────────────────────────────────────────────────
# The preview needs no privilege and no tools: it only prints what the real run
# would check and do, so anyone reviewing the plan can run it.
require_root() {
    if [ "${DRY_RUN}" -eq 1 ]; then
        plan "check: running as root"
        return
    fi
    [ "$(id -u)" -eq 0 ] || die "must run as root: this manages a system service, a system user and /etc + /var/lib paths"
}

require_tools() {
    if [ "${DRY_RUN}" -eq 1 ]; then
        plan "check: git is installed"
        plan "check: uv is installed"
        return
    fi
    command -v git >/dev/null || die "git is required"
    command -v uv >/dev/null || die "uv is required (https://docs.astral.sh/uv/); install it first, this script will not"
}

# The unit runs under ProtectSystem=strict with ${STATE_DIR} as its one writable
# path, so a warm-standby export anywhere else fails for the life of the service
# — and app/standby.py logs every export failure and carries on by design, so
# nothing stops, and `promote` on the standby host finds nothing. Refuse instead.
#
# This greps the deployed TOML rather than parsing it: it reads an uncommented
# `standby_path = "..."` on a line of its own and nothing else. A value inside a
# multi-line string, or one set only through FALLOW_COORD_STANDBY_PATH, is not
# caught here — that is the honest limit of a bash installer that must not import
# the coordinator before its venv exists.
check_standby_path() {
    local value
    [ -f "${CONFIG_DST}" ] || return 0
    value="$(sed -n 's/^[[:space:]]*standby_path[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' \
        "${CONFIG_DST}" | tail -n 1)"
    [ -n "${value}" ] || return 0
    case "${value}" in "${STATE_DIR}"/*) return 0 ;; esac
    if [ "${ALLOW_EXTERNAL_STANDBY}" -eq 1 ]; then
        log "WARNING: standby_path ${value} is outside ${STATE_DIR}; the exports only work if the unit's ReadWritePaths covers $(dirname "${value}")"
        return 0
    fi
    die "standby_path ${value} in ${CONFIG_DST} is outside ${STATE_DIR}, which ${UNIT_NAME} makes read-only (ProtectSystem=strict): every export would fail silently and a failover would find no snapshot. Either move it under ${STATE_DIR}, or widen the unit — systemctl edit ${UNIT_NAME}, [Service] ReadWritePaths=$(dirname "${value}") — and re-run with --allow-external-standby"
}

# Would the service start with an admin key someone chose? The published example
# key is not a key: a coordinator serving with it is one anybody who has read
# this repository can administer. Config-file presence is not the question —
# an operator can edit host and the TLS paths and leave admin_key untouched — so
# this reads the effective value: an uncommented FALLOW_COORD_ADMIN_KEY in the
# unit's environment file (which wins over the config), else an uncommented
# admin_key in the config.
#
# Like check_standby_path this greps rather than parses, and has the same honest
# limits: a value inside a multi-line string, one injected by a systemd drop-in
# this script did not write, or one exported into the service some other way is
# not seen. It errs toward refusing to start, which costs the operator one
# command, never toward starting with the published key. Sets PLACEHOLDER_KEY
# and EMPTY_ENV_KEY.
check_admin_key() {
    local env_line="" from_env="" from_file="" env_has_key=0
    PLACEHOLDER_KEY=1
    EMPTY_ENV_KEY=0
    if [ -f "${ENV_DST}" ]; then
        # The last uncommented assignment is the one systemd honours, empty or
        # not. Presence and value are read separately: a value-matching sed
        # alone would read FALLOW_COORD_ADMIN_KEY= as "no assignment" and fall
        # through to the config, while systemd still injects the empty variable.
        env_line="$(sed -n '/^[[:space:]]*FALLOW_COORD_ADMIN_KEY[[:space:]]*=/p' \
            "${ENV_DST}" | tail -n 1)"
        if [ -n "${env_line}" ]; then
            env_has_key=1
            from_env="$(printf '%s\n' "${env_line}" \
                | sed -n 's/^[[:space:]]*FALLOW_COORD_ADMIN_KEY[[:space:]]*=[[:space:]]*\(.*[^[:space:]]\)[[:space:]]*$/\1/p')"
            # systemd strips matched surrounding quotes when it reads an
            # environment file, so compare the value the service will see:
            # FALLOW_COORD_ADMIN_KEY="<placeholder>" is still the placeholder.
            case "${from_env}" in
                \"*\") from_env="${from_env#\"}"; from_env="${from_env%\"}" ;;
                \'*\') from_env="${from_env#\'}"; from_env="${from_env%\'}" ;;
            esac
        fi
    fi
    if [ -f "${CONFIG_DST}" ]; then
        from_file="$(sed -n 's/^[[:space:]]*admin_key[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' \
            "${CONFIG_DST}" | tail -n 1)"
    fi
    # The env file wins over the config, so when it sets the variable at all it
    # is the effective key and decides on its own — including when what it
    # carries is the placeholder pasted out of the example, and including an
    # empty assignment: systemd supplies the empty variable, the config loader
    # overlays it over the file's admin_key, and min_length=1 then holds the
    # service in a restart loop. Falling through to the config in either case
    # would enable a service that cannot serve with the key it appears to have.
    if [ "${env_has_key}" -eq 1 ]; then
        if [ -z "${from_env}" ]; then
            EMPTY_ENV_KEY=1
        elif [ "${from_env}" != "${PLACEHOLDER_ADMIN_KEY}" ]; then
            PLACEHOLDER_KEY=0
        fi
    elif [ -n "${from_file}" ] && [ "${from_file}" != "${PLACEHOLDER_ADMIN_KEY}" ]; then
        PLACEHOLDER_KEY=0
    fi
    if [ "${EMPTY_ENV_KEY}" -eq 1 ]; then
        NO_START=1
        [ "${DRY_RUN}" -eq 0 ] || plan "check: FALLOW_COORD_ADMIN_KEY is set but empty, so the unit is installed but not started"
    elif [ "${PLACEHOLDER_KEY}" -eq 1 ]; then
        NO_START=1
        [ "${DRY_RUN}" -eq 0 ] || plan "check: no admin key of your own is set, so the unit is installed but not started"
    else
        [ "${DRY_RUN}" -eq 0 ] || plan "check: an admin key is set, so the service starts"
    fi
}

# Is there a service to protect? Under --dry-run systemd cannot be asked, so the
# plan reports what a host carrying this unit would do, keyed on the unit file.
unit_is_running() {
    [ -f "${UNIT_DST}" ] || return 1
    if [ "${DRY_RUN}" -eq 1 ]; then return 0; fi
    command -v systemctl >/dev/null || return 1
    systemctl is-active --quiet "${UNIT_NAME}"
}

# ── Install steps, in the order do_install runs them ─────────────────────────
# The ordering is the interesting part and it is not free-form: the service stops
# before its code is rewritten, the checkout lands before the venv is built from
# it, and the unit is installed last. Each step is named so that order reads off
# do_install itself.

# Probe the ref before a user or a checkout exists: a typo costs nothing on the
# host. The unit file's presence is still checked after the checkout, since only
# the tree can answer that.
#
# REF_FULL, not REF: an unqualified name matches refs/tags AND refs/heads, so a
# branch called v0.3.0 would satisfy the pinned-tag check and then move under the
# machine — exactly what --allow-branch exists to make someone type. The shape of
# the ref decides which namespace is asked for, and nothing else is accepted.
# (untested — verify on target: this reaches the network.)
probe_ref() {
    if [ "${DRY_RUN}" -eq 1 ]; then
        plan "git ls-remote --exit-code ${REPO_URL} ${REF_FULL}"
    else
        git ls-remote --exit-code "${REPO_URL}" "${REF_FULL}" >/dev/null \
            || die "no ref ${REF_FULL} on ${REPO_URL}; nothing was created on this host"
    fi
}

# Stop a running service before its code is rewritten, and record that it was
# running so a --no-start run can say it left it down.
# (untested — verify on target: this reaches systemd.)
stop_if_running() {
    RESTART_AFTER=0
    if unit_is_running; then
        RESTART_AFTER=1
        run systemctl stop "${UNIT_NAME}"
    fi
}

# No login shell: nothing should ever log in as the coordinator.
#
# The group is created explicitly rather than left to useradd. Whether useradd
# makes a same-named group is a distribution setting (USERGROUPS_ENAB in
# login.defs, GROUP in /etc/default/useradd), so on a host configured the other
# way the user lands in `users` and no `fallow` group exists — after which
# `install -g fallow` fails and the unit's Group=fallow cannot resolve. Both
# failures land mid-install, well past the point where nothing has been written.
# groupadd --system then useradd --gid gives the same result on every
# distribution and is the one shape that does not depend on that setting.
ensure_system_user() {
    if getent group "${SERVICE_USER}" >/dev/null 2>&1; then
        log "system group ${SERVICE_USER} already exists"
    else
        run groupadd --system "${SERVICE_USER}"
    fi
    if id -u "${SERVICE_USER}" >/dev/null 2>&1; then
        log "system user ${SERVICE_USER} already exists"
        # A user this script did not create may sit in some other group. The
        # unit still runs Group=${SERVICE_USER} and the state directory is
        # owned by it, so the install works — but say so, because an unrelated
        # account of the same name is worth knowing about before it serves.
        case " $(id -nG "${SERVICE_USER}" 2>/dev/null) " in
            *" ${SERVICE_USER} "*) ;;
            *) log "WARNING: the existing ${SERVICE_USER} user is not in the ${SERVICE_USER} group; the unit runs as User=${SERVICE_USER} Group=${SERVICE_USER} regardless, so check this is the account you meant" ;;
        esac
    else
        nologin="/usr/sbin/nologin"
        [ -x "${nologin}" ] || nologin="/sbin/nologin"
        [ -x "${nologin}" ] || nologin="/bin/false"
        run useradd --system --gid "${SERVICE_USER}" --home-dir "${STATE_DIR}" --shell "${nologin}" "${SERVICE_USER}"
    fi
}

# (untested — verify on target: this reaches the network.)
checkout_at_ref() {
    run install -d -m 0755 "$(dirname "${SRC_DIR}")"
    if [ -d "${SRC_DIR}/.git" ]; then
        log "updating the existing checkout at ${SRC_DIR}"
    else
        run git clone "${REPO_URL}" "${SRC_DIR}"
    fi
    # Fetch the one fully-qualified ref and check out its commit detached: the
    # deployed tree is the ref that was asked for, not a branch that can move
    # under it and not a same-named ref from the other namespace.
    run git -C "${SRC_DIR}" fetch --tags --prune origin "${REF_FULL}"
    run git -C "${SRC_DIR}" checkout --force --detach FETCH_HEAD

    if [ "${DRY_RUN}" -eq 0 ]; then
        [ -f "${UNIT_SRC}" ] || die "no unit file at ${UNIT_SRC}; is ${REF} old enough to predate it?"
        [ -f "${CONFIG_SRC}" ] || die "no example config at ${CONFIG_SRC}"
    fi
}

# --frozen: install exactly uv.lock, never re-resolve on the school server.
# --no-dev: the coordinator host has no use for the test and lint toolchain.
# UV_PYTHON_INSTALL_DIR: the managed CPython goes to ${PYTHON_DIR}, where the
# service user can traverse to it (see the comment at its definition). A re-run
# after an install that used uv's /root default finds the venv linked against
# an interpreter in the old place; uv resolves the pin against this directory
# and rebuilds the venv against what it finds or downloads here.
# (untested — verify on target: this reaches the network.)
sync_venv() {
    run install -d -m 0755 "${PYTHON_DIR}"
    run env UV_PYTHON_INSTALL_DIR="${PYTHON_DIR}" uv sync --frozen --no-dev --project "${SRC_DIR}"
}

# State is the service user's. /etc/fallow is root's, group-readable by the
# service, because it holds the admin key and the Site Mode TLS key.
install_state_and_config() {
    run install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 0750 "${STATE_DIR}"
    # `install -d` takes the directory and nothing inside it. The migration this
    # script is for — a coordinator run in the foreground by root or an operator,
    # now becoming a service — leaves a state tree owned by whoever ran it: the
    # SQLite DB, the blobs, the units and results, the JSONL logs. The service
    # would start, fail its first write, and restart on a loop. This is the same
    # call the config already gets one branch down (the contents are the
    # operator's, the ownership is not), and the same one --purge deletes
    # wholesale, so the directory is not being claimed here for the first time.
    # chown -R does not follow symlinks (-P is its default), so a link in the
    # tree is retargeted at most to itself, never traversed out of ${STATE_DIR}.
    run chown -R "${SERVICE_USER}:${SERVICE_USER}" "${STATE_DIR}"
    run install -d -o root -g "${SERVICE_USER}" -m 0750 "${CONFIG_DIR}"
    if [ "${SEEDING_CONFIG}" -eq 0 ]; then
        log "keeping the existing config ${CONFIG_DST}"
        # The contents are the operator's; the ownership and mode are not. A
        # root:root 0600 copy the service user cannot read fails the start, and a
        # 0644 one hands the admin key and the TLS key path to every local
        # account. Both are silent until they bite, so normalise to what a seeded
        # config gets. Nothing in the file is touched.
        run chown root:"${SERVICE_USER}" "${CONFIG_DST}"
        run chmod 0640 "${CONFIG_DST}"
    else
        run install -o root -g "${SERVICE_USER}" -m 0640 "${CONFIG_SRC}" "${CONFIG_DST}"
        log "copied the example config -> ${CONFIG_DST}"
        log "EDIT IT before this is useful: admin_key (or set FALLOW_COORD_ADMIN_KEY), host (the exact address to serve on), and under [site] tls_certfile and tls_keyfile for a Site Mode pilot"
        log "a TLS key under ${CONFIG_DIR} must be group-readable by ${SERVICE_USER}, or the service cannot start"
    fi
}

# The unit's EnvironmentFile. Without it the FALLOW_COORD_* overrides this
# project documents never reach a service started by systemd, which reads none of
# the operator's shell environment: the advertised way to keep the admin key out
# of the config file silently did nothing.
#
# root:root 0600, not root:fallow 0640 like the config. systemd reads
# EnvironmentFile as PID 1, before it drops to User=fallow, and injects the
# values into the process, so the service user never opens this file. For one
# that exists to hold the admin key, the tightest mode that works is the right
# one. An existing file is normalised the same way and never rewritten.
install_env_file() {
    if [ -f "${ENV_DST}" ]; then
        log "keeping the existing ${ENV_DST}"
    elif [ "${DRY_RUN}" -eq 1 ]; then
        plan "write ${ENV_DST} with a commented FALLOW_COORD_ADMIN_KEY="
    else
        cat > "${ENV_DST}" <<'ENVFILE'
# Environment for fallow-coordinator.service. systemd reads this as root before
# the service drops to the fallow user; one KEY=value per line, no `export`.
#
# Setting the admin key here keeps it out of /etc/fallow/coordinator.toml, which
# is group-readable by the service user. It wins over the file's admin_key.
#FALLOW_COORD_ADMIN_KEY=
ENVFILE
        log "created ${ENV_DST}; set FALLOW_COORD_ADMIN_KEY there to keep the key out of the config"
    fi
    run chown root:root "${ENV_DST}"
    run chmod 0600 "${ENV_DST}"
}

# (untested — verify on target: these reach systemd.)
install_and_start_service() {
    run install -m 0644 "${UNIT_SRC}" "${UNIT_DST}"
    run systemctl daemon-reload
    if [ "${NO_START}" -eq 1 ]; then
        if [ "${EMPTY_ENV_KEY}" -eq 1 ]; then
            # Named first: editing the config cannot fix this one. systemd
            # injects the empty variable over whatever admin_key the file holds.
            log "installed ${UNIT_DST} but did NOT start it: ${ENV_DST} sets FALLOW_COORD_ADMIN_KEY to an empty value, which systemd injects over any admin_key in ${CONFIG_DST} and the service then fails validation on a restart loop. Give it a value or comment the line out, then: systemctl enable --now ${UNIT_NAME}"
        elif [ "${SEEDING_CONFIG}" -eq 1 ]; then
            log "installed ${UNIT_DST} but did NOT start it: ${CONFIG_DST} still holds the example's published placeholder admin key. Set admin_key there, or FALLOW_COORD_ADMIN_KEY in ${ENV_DST}, then: systemctl enable --now ${UNIT_NAME}"
        elif [ "${PLACEHOLDER_KEY}" -eq 1 ]; then
            log "installed ${UNIT_DST} but did NOT start it: no admin key of your own is set. Neither FALLOW_COORD_ADMIN_KEY in ${ENV_DST} nor admin_key in ${CONFIG_DST} carries anything but an empty value or the example's published placeholder. Set one of them, then: systemctl enable --now ${UNIT_NAME}"
        else
            log "installed ${UNIT_DST}; --no-start given, so start it yourself: systemctl enable --now ${UNIT_NAME}"
        fi
        if [ "${RESTART_AFTER}" -eq 1 ]; then
            log "the service was stopped for this run and is still stopped"
        fi
    else
        # restart, not start: a re-run with a newer --ref must pick up the new code.
        run systemctl enable "${UNIT_NAME}"
        run systemctl restart "${UNIT_NAME}"
        log "status: systemctl status ${UNIT_NAME}    logs: journalctl -u ${UNIT_NAME} -f"
    fi
}

do_install() {
    [ -n "${REF}" ] || die "--ref <vX.Y.Z> is required; a pilot deploys a pinned release tag (docs/releasing.md)"
    [ "${PURGE}" -eq 0 ] || die "--purge is an uninstall option"
    # A branch moves under the machine. Pilots deploy tags; anything else is an
    # explicit, argued-for choice.
    if [[ "${REF}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?$ ]]; then
        REF_FULL="refs/tags/${REF}"
    elif [ "${ALLOW_BRANCH}" -eq 1 ]; then
        REF_FULL="refs/heads/${REF}"
    else
        die "refusing an unpinned ref: ${REF} (deploy a vX.Y.Z release tag, or pass --allow-branch)"
    fi

    require_root
    require_tools
    check_standby_path

    # A run that seeds the config seeds the published placeholder admin key with
    # it, so it installs the unit and stops there. The next run — config present,
    # edited — starts the service normally.
    SEEDING_CONFIG=0
    if [ ! -f "${CONFIG_DST}" ]; then
        SEEDING_CONFIG=1
        NO_START=1
    fi
    check_admin_key

    probe_ref
    stop_if_running
    ensure_system_user
    checkout_at_ref
    sync_venv
    install_state_and_config
    install_env_file
    install_and_start_service
}

do_uninstall() {
    [ -z "${REF}" ] || die "--ref is an install option"
    require_root

    # (untested — verify on target: this reaches systemd.)
    run systemctl disable --now "${UNIT_NAME}" || true
    run rm -f "${UNIT_DST}"
    run systemctl daemon-reload
    run rm -rf "${SRC_DIR}" "${PYTHON_DIR}"
    if [ "${PURGE}" -eq 1 ]; then
        run rm -rf "${STATE_DIR}" "${CONFIG_DIR}"
        log "purged ${STATE_DIR} and ${CONFIG_DIR}"
    else
        log "preserved ${STATE_DIR} and ${CONFIG_DIR}; re-run with --purge to delete them"
    fi
    log "the ${SERVICE_USER} system user is left in place; remove it by hand if you are done with this host"
}

case "${VERB}" in
    install)   do_install ;;
    uninstall) do_uninstall ;;
esac
