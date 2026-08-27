# the coordinator is a service, not a terminal someone left open

## Status

Proposed

## Date

2026-08-26

## Goal

Make the coordinator survive a reboot without a person present. Today the school
server needs a git checkout, uv, `uv sync`, and someone typing `uv run python -m
fallow_coordinator serve --config …` into a terminal that has to stay open.
`deploy/README.md` said so plainly (managing it as a service was "left to the
operator in v0.1"), and every desk in the pilot depends on that one process. A
power cut on a Sunday is a fleet outage until somebody drives in.

## Owned paths

- `deploy/coordinator/install.sh` (new)
- `deploy/coordinator/fallow-coordinator.service` (new)
- `tests/deploy/test_coordinator_install.py` (new)
- `deploy/README.md` (§0 support matrix, §3, §6 file table)
- `deploy/coordinator.example.toml` (the `standby_path` note)
- `docs/lan-site/operator-runbook.md` (§2)
- `docs/adr/100-coordinator-systemd-install.md`, `CHANGELOG.md`

No coordinator code is touched. Nothing about how the process runs changes; only
what starts it.

## Decision

**A systemd unit and one install command, pinned to a release tag.**

```bash
sudo deploy/coordinator/install.sh --ref v0.3.0
```

It creates a `fallow` system group and a `fallow` system user with no login
shell, checks the repository out at that ref under `/opt/fallow/src`, builds the
venv with `uv sync --frozen --no-dev`, creates `/var/lib/fallow`
(`fallow:fallow`, `0750`) for state and `/etc/fallow` (`root:fallow`, `0750`) for
config, installs `fallow-coordinator.service`, and starts it.

The group is created explicitly, with `groupadd --system` and a `useradd --gid`
that names it, rather than left to `useradd`'s own behaviour. Whether `useradd`
makes a same-named group is a distribution setting (`USERGROUPS_ENAB` in
`login.defs`, `GROUP` in `/etc/default/useradd`), and on a host configured the
other way the user lands in `users`, no `fallow` group exists, and both `install
-g fallow` and the unit's `Group=fallow` fail — mid-install, well past the point
where nothing had been written. A `fallow` user that predates this script and
sits in some other group still works, because the unit sets the group itself; it
gets a warning, not a refusal.

**`--ref` is required and must be a `vX.Y.Z` tag.** `docs/releasing.md` already
says a pilot deploys a pinned release, not `main`; this makes the installer
enforce it rather than describe it. `--allow-branch` exists for development and
has to be typed on purpose.

**Re-running it is the upgrade.** Stop the running service, fetch, check out the
new ref detached, re-sync, start. There is no separate upgrade verb to keep in
step with the install path, and no state migration to run: an upgrade reads
nothing in the state directory and writes nothing to it beyond re-asserting the
ownership every run asserts. The stop is not cosmetic ordering: the venv imports the
coordinator from `/opt/fallow/src`, so a checkout under a live process swaps the
program out from under it, and the window between `git checkout` and `systemctl
restart` is a coordinator running half of one release and half of another. The
run that seeded the config is the one exception: it never started a service, so
there is nothing to stop or start.

**The ref is probed before the host is touched, in its own namespace.** `git
ls-remote --exit-code` runs before the system user and the clone, so a mistyped
`--ref` costs nothing and leaves nothing behind. Both the probe and the fetch ask
for a fully-qualified ref — `refs/tags/vX.Y.Z`, or `refs/heads/<name>` on the
`--allow-branch` path — because an unqualified name matches either namespace: a
branch named `v0.3.0` would satisfy the pinned-tag check and then move under the
machine, which is the exact thing `--allow-branch` exists to make someone type.
The shape of the ref picks the namespace and nothing else is accepted. Only the
tree can say whether that ref carries a unit file and an example config, so those
two checks stay where they were, after the checkout.

**The operator's config is never overwritten, and its mode is not left to
chance.** `coordinator.example.toml` is copied to `/etc/fallow/coordinator.toml`
only when that file is absent, the same rule `deploy/macos/install.sh` follows
for `agent.toml`. On the copy the script names the keys that must be edited
(`admin_key`, `host`, and the `[site]` certificate paths). An existing config
keeps every byte, but its ownership and mode are reset to the `root:fallow 0640`
a seeded one gets: a `root:root 0600` copy fails the `User=fallow` read at start,
a `0644` one hands the admin key to every local account, and both are silent
until they bite.

**A preserved state tree is handed to the service user, not only its directory.**
`install -d` sets the directory and nothing inside it, and the migration this
installer exists for is a coordinator that ran in the foreground: the SQLite DB,
the blobs, the units and results, and the JSONL logs are all owned by whoever ran
it, usually root. The service would start, fail its first write, and restart on a
loop. So `/var/lib/fallow` gets a `chown -R fallow:fallow` — the same call the
config gets one line down, on the same reasoning that the contents are the
operator's and the ownership is not, and on the same directory `--purge` deletes
wholesale. `chown -R` does not follow symlinks (`-P` is its default), so a link
inside the tree is retargeted at most to itself and never traversed out of the
state directory. The alternative was to detect foreign ownership and refuse: it
would cost the operator one command and buy nothing, since the script already
takes the directory itself unconditionally.

**The start gate is the admin key, not the config file.** File presence was the
wrong question. An operator who edits `host` and the TLS paths and leaves
`admin_key = "change-me-to-a-long-random-string"` alone got a running coordinator
administered by a key published in this repository. The script now reads the
effective key — an uncommented `FALLOW_COORD_ADMIN_KEY` in the environment file,
which wins, else an uncommented `admin_key` in the config — and where that is
empty or still the placeholder it installs the unit, does not start it, and says
which of the two to set. The environment file is held to the same test as the
config, and decides alone once it sets the variable: it is what the service will
actually run on, so the placeholder pasted into it is the published key whatever
the config says. That covers the seeding run, which was the only case the
old check caught. It is the same grep-not-parse compromise as `standby_path`,
with the same honest limits: a value in a multi-line string, or one injected by a
drop-in this script did not write, is not seen. It errs toward refusing to start,
which costs one command; the other direction costs the pilot its admin API.

**`FALLOW_COORD_*` overrides need an `EnvironmentFile` to exist.** The unit had
none, so the override this project documents everywhere — set
`FALLOW_COORD_ADMIN_KEY` instead of putting the key in the config — reached
nothing once the coordinator ran under systemd, which inherits no shell
environment. `EnvironmentFile=-/etc/fallow/coordinator.env` fixes that; the
leading `-` keeps a host without the file starting. The installer seeds it, if
absent, with that one line commented out, and normalises it to `root:root 0600`
— not the config's `root:fallow 0640`. systemd reads `EnvironmentFile` as PID 1
and injects the values, so the service user never opens it, and the whole point
of the file is to hold a key the config would expose to that user. The tightest
mode that works is the right one.

**A standby_path the unit cannot write is refused.** `ProtectSystem=strict` plus
`ReadWritePaths=/var/lib/fallow` means an export to `/mnt/standby` fails every
time, and `app/standby.py` logs each failure and keeps the loop alive by design,
so the failure mode is a coordinator that looks healthy and a failover that finds
no snapshot. The installer greps the deployed config for an uncommented
`standby_path` and refuses one outside `/var/lib/fallow`, naming the drop-in that
fixes it (`systemctl edit fallow-coordinator.service`, a Service section with
`ReadWritePaths=/mnt/standby`). `--allow-external-standby` is the way to say the
drop-in is already there. It is a grep, not a TOML parse: a path set only through
`FALLOW_COORD_STANDBY_PATH`, or inside a multi-line string, is not caught, and
the script says so where it does it. A bash installer that must not import the
coordinator before its venv exists has no better honest option.

**The unit is copied verbatim from the checkout, not rendered.** Every path in
it is a fixed system path, so there is nothing to substitute, and a file with no
template seam is a file that cannot be rendered wrong. It is read from
`/opt/fallow/src` after the checkout rather than from beside the script, which
means the unit always matches the ref being deployed and the script can be
curled on its own from a release tag.

**Hardening, kept small.** `NoNewPrivileges`, `ProtectSystem=strict` with
`ReadWritePaths=/var/lib/fallow`, and `PrivateTmp`. Those are the directives
that pay for themselves against this process: it never needs to gain privilege,
it writes to exactly one directory (SQLite, blobs, unit inputs, results, the
JSONL logs), and its temp files are its own. `ProtectHome` is deliberately
absent: an operator may register a model blob from a path under `/home`, and a
coordinator that cannot read the file it was pointed at is a much worse failure
to diagnose than the exposure it would close. A long hardening block copied from
a blog post would be directives nobody here has reasoned about.

**uv is a prerequisite, not something the script installs.** CI pins uv through
`astral-sh/setup-uv`; the honest alternatives for a script are a pinned download
with a checksum to maintain, or piping an unpinned installer into a root shell
on a school server. Neither is worth it to save one `apt install`. The script
checks for uv and git and stops with a plain message.

**This path needs egress.** `uv sync --frozen` re-resolves nothing, but it does
download every wheel it has not cached *and* a managed CPython 3.12: the
workspace sets `python-preference = "only-managed"`, so a system python on the
host is not used even when it is the right version. Together with the `git clone`
that means github.com and PyPI must be reachable from the coordinator host. A
zero-egress lab cannot install this way and uses the offline bundle
(`deploy/bundle.sh`, `deploy/OFFLINE.md`) instead. The managed interpreter
installs under `/opt/fallow/python` (`UV_PYTHON_INSTALL_DIR`), not uv's default
in the invoking user's private data directory: on a root install that default
sits behind a 0700 `/root`, the venv's `python` is a symlink into it, and
`User=fallow` could never exec its own `ExecStart`.

**`--dry-run` prints the plan and touches nothing**, the seam
`deploy/bootstrap.sh` and `deploy/macos/install.sh` already carry. It needs no
root, because a preview that requires the privilege it is previewing cannot be
reviewed before it is trusted.

**`uninstall` stops and disables the unit, removes it and `/opt/fallow/src`, and
keeps `/etc/fallow` and `/var/lib/fallow`** unless `--purge`. State and a hand-
edited config are the two things an operator cannot recreate; a removal that
takes them by default is a removal nobody runs. The `fallow` user is left in
place and the script says so.

## Verification

`tests/deploy/test_coordinator_install.py` drives the `--dry-run` plan and
asserts it names every action in order, that a missing `--ref`, an unpinned ref
and a `--ref` that swallowed the next flag are all refused, that `--allow-branch`
lifts the second, that `uninstall`
keeps state unless purged, and that the preview leaves `/opt/fallow`,
`/etc/fallow`, `/var/lib/fallow` and the unit path exactly as it found them. A
tag ref is asserted to be probed and fetched as `refs/tags/…` and never as the
bare name, a branch ref as `refs/heads/…`.

The admin-key gate is pinned in both directions: a config carrying the
placeholder, an empty key, no key at all, or the placeholder with surrounding
whitespace all install the unit without starting it, and an uncommented
`FALLOW_COORD_ADMIN_KEY` in the environment file starts the service even when the
config still holds the placeholder, while a commented, empty, or placeholder-
valued one does not, whatever the config carries. The
environment file is asserted to be seeded and normalised root-only, an existing
one normalised and never rewritten, and an existing config to be `chown`/`chmod`ed
without being reinstalled. The unit is parsed and checked directive by directive
— including the `EnvironmentFile` that makes the override reach the service — and
run through `systemd-analyze verify` where that tool exists. The one complaint tolerated
there is the missing `ExecStart` binary, since the venv is built by the install
that has not run. The unit is copied, not rendered, so one test asserts the
script and the unit still agree on the paths.

The branches that read the host (a config with an out-of-sandbox
`standby_path`, a first run that must not start the service, an upgrade over an
installed unit) are exercised through one seam: `FALLOW_INSTALL_ROOT` prefixes
every system path, so the tests build a fake `/etc/fallow` and
`/etc/systemd/system` in a temporary directory and read the plan that comes out.
It is empty in every real run.

## Compatibility

Additive. The manual `uv run python -m fallow_coordinator serve` path is
unchanged and stays documented: it is still the macOS path and the development
path. No coordinator behaviour, config key or wire format is affected.

## Exclusions and honest gaps

**Linux only.** macOS coordinators keep the manual path. A `launchd` plist for
the coordinator would be a second file and a second install script for a machine
class no pilot currently runs, so it waits for a pilot that needs it.

**Not executed on a systemd host.** This was authored in a sandbox with no
systemd PID 1. The unit parses and `systemd-analyze verify` accepts it, and the
plan is asserted end to end, but the clone, the venv build and the `systemctl`
calls have never run for real: they carry the same `(untested — verify on
target)` marks as the rest of `deploy/`. First boot on the school server is a
pilot-day check.

**No container image.** A published image would remove the git checkout and uv
from the host entirely, which is the better long-run answer. It also needs a
registry, a signing story and a base-image update policy that this project does
not have yet, and the pilot host is a machine the school already administers.
Deferred until there is somewhere to publish to that a school would trust.

**No log rotation or backup wiring.** The coordinator's own JSONL logs grow
under `/var/lib/fallow` and nothing here prunes them, and nothing here backs up
`coordinator.db`. Warm standby (ADR 054/057) remains the availability story and
is configured by hand.
