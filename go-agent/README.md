# Go agent

This module contains the Go implementation of the managed Fallow agent. E4.1
establishes its protocol package; later E4 work adds the daemon and platform
integrations.

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
