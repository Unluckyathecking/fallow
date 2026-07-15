# Go agent

This module contains the Go implementation of the managed Fallow agent. It uses
the coordinator's existing HTTP and JSON contract. The Python coordinator does
not need a separate route or protocol version for Go agents.

The JSON Schemas in [`../schemas/`](../schemas/) are the wire contract. Run the
generator from the repository root after a schema change:

```bash
python3 schemas/generate_go.py
```

The generator writes `protocol/types_gen.go` and formats it with `gofmt`. Do not
edit that file by hand. It maps JSON arrays to Go slices, nullable values to
pointers, schema date-time strings to `time.Time`, and string enums to named Go
types with constants.

Both protocol implementations read the JSON files in
[`../schemas/fixtures/`](../schemas/fixtures/). Run the conformance tests with:

```bash
go test ./...
```

`python3 schemas/generate_go.py --check` exits with an error when the committed
Go output does not match the schemas.

## Core packages

- `coordinator` handles enrollment, authenticated heartbeats, event pushes,
  long-poll work requests, and result completion. Heartbeat and work polls retry
  transport errors only. HTTP 5xx responses are classified as transient and
  returned to the caller without an inline retry.
- `daemon` contains the heartbeat loop and the queued event sink. The sink
  appends each event to `events.jsonl` before its best-effort coordinator push.
- `idle` reads `GetLastInputInfo` on Windows and Core Graphics on macOS. The
  Linux implementation returns a clear unsupported error because X11, Wayland,
  and logind do not share one correct idle source.
- `preempt` contains the polling goroutine and state machine. Its user-return
  path calls `SuspendAll` before measuring latency or emitting an event.

The identity file contains the agent ID and bearer device token. The writer
uses a temporary file and atomic replacement. Unix files are set to `0600`.
Windows mode bits are advisory, so deployments rely on the user's profile
directory ACL to protect the file.

The platform implementations are selected with Go build tags. macOS builds
need cgo and the system ApplicationServices framework. Windows uses
`golang.org/x/sys/windows`. Run all Go tests, including the race detector, with:

```bash
go test -race ./...
```
