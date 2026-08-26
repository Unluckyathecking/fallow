# ship idle detection in the released macOS agent

## Status

Proposed

## Date

2026-08-26

## Goal

Make the released Go agent keep the project's one promise: yield the machine the
moment a person touches it. Today the shipped macOS binary cannot. Two accepted
records disagree and nothing reconciles them — [ADR 037](037-go-core-daemon.md)
made `darwin && !cgo` an honest `ErrUnsupported` stub, and
[ADR 041](041-go-agent-release.md) then shipped every target with
`CGO_ENABLED=0`, so the stub is what a macOS desk downloads. An unsupported
detector is not inert: the heartbeat reports a fixed 300 s idle forever and the
preempt loop never advances, so the machine reads as permanently idle, serves
through the user's whole working day, and never yields.

## Owned paths

- `go-agent/config/config.go`
- `go-agent/runtime/runtime.go`
- `go-agent/runtime/seams.go`
- `go-agent/cmd/agentctl/main.go` (the `doctor` idle lane)
- `go-agent/runtime/idlegate_test.go` (new)
- `go-agent/config/config_test.go`
- `go-agent/.goreleaser.yaml`
- `.github/workflows/release.yml`
- `tests/integration/goagent.py`
- `tests/integration/site_mode/site_harness.py`
- `deploy/agent.example.toml`, `deploy/README.md`, `deploy/windows/doctor.ps1`
- `docs/lan-site/operator-runbook.md` (the doctor lane list)
- `docs/adr/098-go-idle-fail-closed.md`, `docs/adr/041-go-agent-release.md`
- `CHANGELOG.md`

No other path belongs to this change. The detectors themselves, the preempt
state machine and the wire schema are untouched.

## Decision

**Fail closed.** `Run` samples the detector once before it enrolls or starts a
loop. If the detector cannot answer, the daemon refuses to start and says why:
this build has no idle detection on this platform, so it would report the
machine permanently idle and never yield to the person using it. A desk that
cannot see its user does not serve. A probe that fails for some other reason is a
different fault and says so rather than blaming the build. `agentctl doctor`
takes the same sample as an `idle` lane, so a desk hears about it before it is
asked to serve rather than at the daemon's first start.

**A failed sample is never idleness.** Once running, a detector that stops
answering reports the machine as in use — 0 seconds since input — and logs once.
The away value belongs to `assume_idle` alone, which is a machine that really has
nobody at it; anywhere else, reading a broken probe as "away" is exactly how a
desk ends up served over while someone types.

**One override, `assume_idle`.** A machine with nobody at the keyboard — the CI
acceptance harnesses, a dedicated headless host — sets `assume_idle = true` in
the same agent TOML and keeps the previous behaviour exactly: the away fallback
in heartbeats, no preemption. It is logged on every start, naming what it costs,
so the state is visible in the daemon's first lines rather than inferred from
its silence. It is the only knob added, and it is never for a machine a person
uses.

**Build darwin natively.** GoReleaser keeps `windows_amd64` and `linux_amd64`,
which need no cgo. `darwin_arm64` leaves the GoReleaser targets and is built on a
`macos-latest` runner with `CGO_ENABLED=1`, which is the only way the Quartz call
compiles in. The macOS job reproduces the GoReleaser archive by hand — same
`fallow-agentctl_<version>_darwin_arm64.tar.gz` name, same binary-plus-README
contents, same `-trimpath` and the same `-X main.version/-X main.commit` stamp —
then appends its `shasum -a 256` line to the `checksums.txt` the GoReleaser job
published and uploads both to the same release. GoReleaser 2.5.0 OSS has no
split/merge, so hand-matching one archive is the whole cost of a native build;
the alternative was a Pro feature or a release that lies about what it detects.
The pull-request path gains the same native build without publishing, so a broken
cgo detector fails before a tag is cut.

## Verification

Go tests cover the gate directly: an unsupported detector refuses to run and
never registers, the error names `assume_idle`, the override permits the start,
and a working detector needs nothing. A transient probe error is refused with its
own message and does not blame the build, and a sample that fails at runtime
reports 0, not the away value. The config test pins that `assume_idle`
defaults off. The three release targets still cross-compile, including the
cgo-less darwin build that now refuses to run.

The acceptance suites are the proof the split works: `tests/integration/site_mode`
and `tests/integration/site_discovery` drive the real `agentctl` on Linux, where
the detector is unsupported, and their harnesses set the one override — so the
lanes stay green while a user's desk fails closed. Both jobs of the release
workflow assert `go list` reports cgo files in `idle` before building, which is
what a silently cgo-less macOS build would fail.

## Compatibility

Wire-compatible and config-compatible: `assume_idle` is absent from every
existing config and defaults off. Windows desks are unaffected — their detector
works — and macOS desks running a cgo build are unaffected. A macOS desk running
a previously released binary will now refuse to start after upgrading rather than
serve through its user's day; that is the fix, not a regression, and the error
says what to install.

## Exclusions and honest gaps

Linux stays unsupported ([ADR 037](037-go-core-daemon.md); a correct detector
needs X11, Wayland and logind sources). A Linux agent is now a headless-only
deployment: it must set `assume_idle`, which is the honest description of what
it was already doing silently.

The GoReleaser job publishes the release before the macOS job builds, so between
them the release is live with the windows and linux archives and a
`checksums.txt` that has no darwin line — and if the macOS job fails it stays
that way. Nothing retracts it: the repair is to re-run that job (it re-downloads
`checksums.txt`, drops any stale darwin line and re-uploads) or to delete the
release and re-tag. Draft-then-publish gated on both jobs would close the window
and is the change to make if it ever bites a real release; restructuring the
publish was not worth it for a two-job gap that repairs by re-running.

The native macOS build cannot be executed in this repository's Linux CI
container or by this change's own verification: that the Quartz detector really
answers on a real desk is a pilot-day check, as it has always been. What is
verified here is that the release pipeline stops shipping a binary that
provably cannot.
