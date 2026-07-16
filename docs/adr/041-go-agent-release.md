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
