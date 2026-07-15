# ADR 007: Model blob serving (module C3)

Status: accepted · Date: 2026-07-15

## Context

Agents must fetch the exact gguf/model artifact they are assigned before they
can launch a replica, and verify sha256 against the manifest (ADR 000: workers
download blobs from the coordinator and verify before first use). Blobs are
multi-GB, downloads happen over office networks, and the agent-side `ModelStore`
resumes partial pulls — so the server must support HTTP Range and must not
buffer whole files in memory.

## Decision

- Ship a `create_modelserve_router(registry)` factory returning a FastAPI
  `APIRouter`, so the coordinator app composes it; the module owns no app.
- The router depends on a narrow `BlobRegistry` `Protocol`
  (`authenticate_agent`, `get_manifest`, `get_model`), not on `SqliteRegistry`.
  This keeps modelserve testable with an in-memory fake and honours the
  `modelserve → registry` layer edge without importing the store's internals.
- `GET /.../blob` requires a device token, honours `Range` (`bytes=N-`,
  `bytes=N-M`, suffix `bytes=-N`) with `206` + `Content-Range`, else `200`;
  streams the file in 1 MiB chunks via `anyio.open_file`. `Content-Length`
  always equals bytes streamed.
- Unknown **and** disabled models both return `404` (no catalogue enumeration
  for holders of a valid device token); malformed/out-of-range `Range` → `416`.
- `GET /.../manifest` returns the `ModelManifest` JSON (auth'd).

## Consequences

- Resumable, bounded-memory downloads; an agent that lost its connection
  re-requests `bytes=<already-have>-` and continues.
- File size is `stat`ed per request (via `anyio.Path`); a catalogued blob whose
  file is missing surfaces as `404` rather than a stream error.
- No integrity check is performed server-side on read — the agent verifies
  sha256 from the manifest, matching the ADR 000 trust model.
- Tests exercise the full ASGI path (`httpx` + `ASGITransport`) against a temp
  file, with no real llama-server, network, or GPU.
