# ADR 013: `flw` CLI + admin API contract (module L1)

Status: accepted · Date: 2026-07-15

## Context

Operators need a command line to enroll agents, mint client keys, register/pull
models, assign models to agents, and submit/inspect batch jobs. The coordinator's
admin HTTP API does not exist yet (wave-3 assembles the app). The CLI must be
built now, deterministically testable, and must not import coordinator/agent code
(it depends on `fallow_protocol` + typer + rich + httpx only).

## Decision

- **The CLI defines the contract.** `docs/admin-api.md` specifies every admin
  route (`/v1/admin/*`, `Authorization: Bearer <admin key>`) referencing
  `fallow_protocol` wire types. Wave-3 implements the server from it. Bodies
  unique to admin ops live in `fallow_cli.models` as frozen `FallowModel`s.
- **`AdminClient` wraps an injected `httpx.Client`** — one typed method per
  route. HTTP transports are module seams (`_ADMIN_TRANSPORT`,
  `_DOWNLOAD_TRANSPORT`) monkeypatched with `httpx.MockTransport`, so tests use
  no real network, llama-server, or GPU.
- **Config resolution is pure and layered**: URL = flag → `FLW_COORDINATOR_URL`
  → `~/.fallow/cli.toml`; admin key = `FLW_ADMIN_KEY` → config file, **never a
  flag** (a flag leaks the secret into shell history and process listings).
- **Failures are friendly**: expected errors are `CliError`s printed to stderr
  with a non-zero exit (`2` for auth/config, `1` otherwise); 401/403 →
  `admin key rejected`, connect error → `coordinator unreachable at <url>`. No
  tracebacks for expected failures.
- **sha256 is computed locally** by streaming the blob; `register`/`pull` build a
  validated `ModelManifest` and POST it with an absolute `blob_path` (v0.1
  assumes the CLI runs on the coordinator host).
- **`--json` is a global flag** rendering every command as machine-readable JSON;
  otherwise rich tables. Download progress renders on stderr to keep stdout clean.

## Consequences

- The contract is fixed before the server exists, so wave-3 codes to a written
  spec and the two halves share wire shapes by construction.
- `blob_path` couples v0.1 registration to a co-located CLI; a multipart upload
  route is the documented v0.2 path (see `docs/admin-api.md` open questions).
- Routes return bare `fallow_protocol` objects (no envelope) to keep the wire
  shape identical to the shared types.
