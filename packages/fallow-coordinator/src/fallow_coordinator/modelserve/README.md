# modelserve — model blob & manifest serving (module C3)

A FastAPI `APIRouter` that streams registered model blobs (with resumable
`Range` requests) and manifests to **authenticated agents**. Agents pull the
gguf they were assigned, verifying sha256 locally against the manifest.

## Public API

Re-exported from `fallow_coordinator.modelserve`:

- `create_modelserve_router(registry: BlobRegistry) -> APIRouter`
- `BlobRegistry` — the narrow `Protocol` the router depends on
  (`authenticate_agent`, `get_manifest`, `get_model`). `SqliteRegistry`
  satisfies it structurally.
- `CHUNK_SIZE` (1 MiB), `OCTET_STREAM`.

### Routes

- `GET /v1/models/{model_id}/blob` — device-token auth. `200` full body or
  `206` partial for `Range: bytes=N-` / `bytes=N-M` / `bytes=-N`, with correct
  `Content-Length` and `Content-Range`. `404` for unknown/disabled model,
  `416` for an unsatisfiable range, `401` without a valid bearer.
- `GET /v1/models/{model_id}/manifest` — device-token auth; returns the
  `ModelManifest` JSON, `404` if unknown.

## Invariants

- Every request requires `Authorization: Bearer <device-token>` resolving via
  `registry.authenticate_agent`; otherwise `401`.
- A model that is absent **or** `enabled = false` is indistinguishable to the
  caller: both `404`.
- Blobs are read in 1 MiB chunks via `anyio.open_file` (never buffered whole),
  so multi-GB artifacts stream with bounded memory.
- `Content-Length` always equals the number of bytes actually streamed (full
  size on `200`, range length on `206`).

## Boundaries

Depends only on the `BlobRegistry` protocol (module C2's `ModelRecord` type),
never on `SqliteRegistry` internals. Tests use `httpx.AsyncClient` +
`ASGITransport` against a temp blob file and an in-memory fake registry — no
network, no real llama-server, no GPU.
