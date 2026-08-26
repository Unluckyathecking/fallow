# the coordinator is a service, not a terminal someone left open

## Status

Proposed

## Date

2026-08-26

## Goal

Make the coordinator survive a reboot without a person present. Today the school
server needs a git checkout, uv, `uv sync`, and someone typing `uv run python -m
fallow_coordinator serve --config …` into a terminal that has to stay open.
`deploy/README.md` said so plainly — managing it as a service was "left to the
operator in v0.1" — and every desk in the pilot depends on that one process. A
power cut on a Sunday is a fleet outage until somebody drives in.

## Owned paths

- `deploy/coordinator/install.sh` (new)
- `deploy/coordinator/fallow-coordinator.service` (new)
- `tests/deploy/test_coordinator_install.py` (new)
- `deploy/README.md` (§0 support matrix, §3, §6 file table)
- `docs/lan-site/operator-runbook.md` (§2)
- `docs/adr/100-coordinator-systemd-install.md`, `CHANGELOG.md`

No coordinator code is touched. Nothing about how the process runs changes; only
what starts it.

## Decision

**A systemd unit and one install command, pinned to a release tag.**

```bash
sudo deploy/coordinator/install.sh --ref v0.3.0
```

It creates a `fallow` system user with no login shell, checks the repository out
at that ref under `/opt/fallow/src`, builds the venv with `uv sync --frozen
--no-dev`, creates `/var/lib/fallow` (`fallow:fallow`, `0750`) for state and
`/etc/fallow` (`root:fallow`, `0750`) for config, installs
`fallow-coordinator.service`, and starts it.

**`--ref` is required and must be a `vX.Y.Z` tag.** `docs/releasing.md` already
says a pilot deploys a pinned release, not `main`; this makes the installer
enforce it rather than describe it. `--allow-branch` exists for development and
has to be typed on purpose.

**Re-running it is the upgrade.** Fetch, check out the new ref detached,
re-sync, restart. There is no separate upgrade verb to keep in step with the
install path, and no state migration to run: the state directory is untouched by
an upgrade.

**The operator's config is never overwritten.** `coordinator.example.toml` is
copied to `/etc/fallow/coordinator.toml` only when that file is absent, the same
rule `deploy/macos/install.sh` follows for `agent.toml`. On the copy the script
names the keys that must be edited — `admin_key`, `host`, and the `[site]`
certificate paths — because an unedited config ships a placeholder admin key.

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
absent — an operator may register a model blob from a path under `/home`, and a
coordinator that cannot read the file it was pointed at is a much worse failure
to diagnose than the exposure it would close. A long hardening block copied from
a blog post would be directives nobody here has reasoned about.

**uv is a prerequisite, not something the script installs.** CI pins uv through
`astral-sh/setup-uv`; the honest alternatives for a script are a pinned download
with a checksum to maintain, or piping an unpinned installer into a root shell
on a school server. Neither is worth it to save one `apt install`. The script
checks for uv and git and stops with a plain message.

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
asserts it names every action in order, that a missing `--ref` and an unpinned
ref are both refused, that `--allow-branch` lifts the second, that `uninstall`
keeps state unless purged, and that the preview leaves `/opt/fallow`,
`/etc/fallow`, `/var/lib/fallow` and the unit path exactly as it found them. The
unit is parsed and checked directive by directive, and run through
`systemd-analyze verify` where that tool exists — the one complaint tolerated
there is the missing `ExecStart` binary, since the venv is built by the install
that has not run. The unit is copied, not rendered, so one test asserts the
script and the unit still agree on the paths.

## Compatibility

Additive. The manual `uv run python -m fallow_coordinator serve` path is
unchanged and stays documented — it is still the macOS path and the development
path. No coordinator behaviour, config key or wire format is affected.

## Exclusions and honest gaps

**Linux only.** macOS coordinators keep the manual path. A `launchd` plist for
the coordinator would be a second file and a second install script for a machine
class no pilot currently runs, so it waits for a pilot that needs it.

**Not executed on a systemd host.** This was authored in a sandbox with no
systemd PID 1. The unit parses and `systemd-analyze verify` accepts it, and the
plan is asserted end to end, but the clone, the venv build and the `systemctl`
calls have never run for real — they carry the same `(untested — verify on
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
