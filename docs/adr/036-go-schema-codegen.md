# ADR 036: Go schema generation and conformance fixtures

**Status:** accepted

**Date:** 2026-07-15

## Context

The managed agent is being ported to Go while the coordinator remains in
Python. The committed JSON Schemas are the language-neutral wire contract.
Maintaining a second set of structs by hand would let the implementations drift,
and separate test samples could hide the same problem.

## Decision

A small Python program in `schemas/generate_go.py` reads every committed schema
and writes one formatted Go source file. It uses only the Python standard
library and the `gofmt` command supplied with Go. Generation is deterministic.

The generator maps JSON arrays to slices, nullable fields to pointers, objects
with typed additional properties to maps, and schema date-time strings to
`time.Time`. String enums become named string types with constants. The output
contains JSON field tags and is committed under `go-agent/protocol/`.

The valid wire examples live in `schemas/fixtures/`. Python and Go tests decode
and encode those same files, then compare the resulting JSON values. The Go CI
workflow checks generated output before running the Go tests. It runs only when
the Go module, schemas, fixtures, generator, or workflow changes.

We rejected a general-purpose code generator because its output and type rules
would add another dependency to the protocol boundary. We also rejected manual
Go structs and separate fixtures because neither approach gives CI a single
source to compare.

## Consequences

- Schema changes must include regenerated Go code.
- A fixture change is exercised by both language implementations.
- The generated structs represent wire shapes and enum values. JSON Schema
  range, pattern, and unknown-field validation remains in the Python models or
  in handwritten Go code where the daemon needs it.
- Running the generator requires Python and the Go toolchain.
