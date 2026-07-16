# ADR 041: Go agent release tooling (E4.5)

**Status:** accepted

**Date:** 2026-07-16

## Context

The Go agent (`go-agent/`, binary `cmd/agentctl`) is built, tested, and passes
the parity harness ([ADR 036](036-go-schema-codegen.md) through
[ADR 038](038-go-supervisor-modelcache.md)). To use it on a real machine an
operator first needs a per-OS binary they can download. This ADR records how that
binary is released.

The original plan for E4.5 also had the deploy scripts install the prebuilt
binary as the running agent. That is deferred: `agentctl` today is the parity
driver — one-shot subcommands (`register`, `heartbeat`, `poll`, `upload`,
`complete`, `version`) with no daemon `run` mode. Wiring launchd or Task
Scheduler to `agentctl run` would fail on launch, so the install path waits on the
Go daemon and is tracked as a follow-up. The deploy scripts keep installing the
Python agent unchanged.

## Decision

### Tag-driven release with GoReleaser

Releases are cut by GoReleaser from a `v*` git tag. The config lives in
`go-agent/.goreleaser.yaml` next to the module's `go.mod`, so GoReleaser runs in
that directory and finds the repo tags by walking up to the root `.git`.

- **Targets:** exactly `darwin/arm64`, `windows/amd64`, `linux/amd64` — the three
  OSes E4 ships. No speculative targets (no `windows/arm64`, no `darwin/amd64`),
  listed explicitly rather than as a matrix-plus-ignore so the set is obvious.
- **Version stamping:** the tag and commit are baked in via
  `-ldflags -X main.version=... -X main.commit=...`, surfaced by an `agentctl
  version` subcommand. A plain `go build` leaves the defaults (`dev`/`none`), so
  the module still builds with no release tooling.
- **Archives:** `tar.gz` per unix OS, `zip` for Windows, plus a `checksums.txt`.
  `CGO_ENABLED=0` and `-trimpath` keep the binaries static and reproducible.
- **Publish:** GitHub Releases only. No signing and no other distribution
  channel in v0.1 — code-signing is deferred alongside the Windows
  Defender/SmartScreen work already noted in the deploy README.

### CI: check and snapshot on PR, release on tag

A new workflow (`.github/workflows/release.yml`) leaves `ci.yml` and `go.yml`
untouched:

- On pull requests that touch `go-agent/`, it runs `goreleaser check` and
  `goreleaser build --snapshot --clean`, uploading the three binaries as an
  artifact. Nothing is published.
- On a `v*` tag it runs `goreleaser release --clean`, which builds the archives
  and creates the GitHub Release.

The GoReleaser version is pinned (`2.5.0`), and the action is pinned by commit
SHA like every other action in this repo. A release must be reproducible from
its tag, so nothing floats to `latest`. Workflow-level concurrency would cancel
in-progress runs on the same ref; it is scoped to the PR snapshot job only, so a
tag release is never cancelled mid-publish.

## Consequences

- CI cross-builds all three OSes on every `go-agent/` PR, so a broken release
  config fails before a tag is ever cut.
- A `v*` tag produces downloadable, checksummed per-OS archives with the version
  stamped in. That is the artifact half of the story.
- Installing the binary as the agent is not part of this change. It depends on a
  Go daemon `run` mode and lands in a follow-up; until then the deploy scripts
  install the Python agent as before.
- Signing and a non-GitHub distribution channel are explicitly out of scope for
  v0.1.

## Addendum (2026-07-16): install path for the Go binary

The deferred half of E4.5 now ships. #62 landed the daemon `run` mode
([ADR 045](045-go-agent-daemon.md)), so `agentctl run -config <path>` is a real
long-running agent and the deploy scripts can install it.

### Decision

The install flavour is opt-in and additive. `deploy/macos/install.sh` gains a
`--go-binary <path>` option and `deploy/windows/install.ps1` a `-GoBinary <path>`
parameter with the same semantics:

- **Default (no flag) is unchanged.** The Python venv path is byte-for-byte what
  it was: resolve the checkout, `uv sync`, run `python -m fallow_agent run`.
- **With the flag** the installer copies the binary to `~/.fallow/bin/agentctl`
  (`agentctl.exe` on Windows) and points the service at it, skipping the
  uv/venv/checkout setup. The `pyproject.toml` and `uv` prerequisites are only
  enforced on the Python path.

Everything else is shared: the same `~/.fallow/agent.toml`, the same
`llama-server` staging, and the same user-session service (LaunchAgent /
Scheduled Task) for the same idle-detection reasons ([ADR 040](040-macos-idle-ctypes.md),
[ADR 044](044-linux-idle-detection.md)).

### Why rewrite the arg vector instead of adding a second template

The plist and task-XML templates ship the Python arg vector
(`… -m fallow_agent run --config …`). Rather than fork a second Go-shaped
template, the installer rewrites that vector at render time: it drops the
`-m fallow_agent` interpreter args and switches the flag to the binary's
single-dash `-config`, leaving `agentctl run -config <path>`. This keeps one
template per OS on disk (no drift between two copies) and confines the whole
flavour difference to the installer. Note the flag style differs by design —
Python's argparse uses `--config`, the Go binary's `flag` package uses `-config`.

### Consequences

- The prebuilt binary is now installable as the agent, closing what #48
  descoped and this ADR originally deferred. `agentctl` no longer needs a repo
  checkout, uv, or a venv on the target.
- The templates stay single-sourced; the render logic is verified by
  `deploy/macos/render_test.sh`, which drives `install.sh`'s dry-run seam and
  asserts each flag selects the expected agent. CI runs no shell lint/test job,
  so this check is run locally.
- Windows has no `pythonw`-style windowless launcher for a console binary, so a
  brief console window at logon is possible with the Go flavour; a windowless
  wrapper is deferred to v0.2 alongside code-signing.
