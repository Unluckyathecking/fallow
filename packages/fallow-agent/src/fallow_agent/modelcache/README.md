# fallow-agent · modelcache

Agent-side model cache. Implements `fallow_protocol.interfaces.ModelStore`:
pull GGUF blobs from the coordinator, resume interrupted downloads, verify
sha256, and keep verified files on local disk.

## Purpose

Every replica needs its model file locally before `llama-server` can launch.
This module is the only thing that talks to the coordinator's blob endpoint. It
is safe to call `ensure()` repeatedly and from multiple tasks at once.

## Public API

Re-exported from `fallow_agent.modelcache`:

- `HttpModelStore` — the `ModelStore` implementation.
  - `path_if_present(manifest) -> Path | None` — synchronous, hot-path safe.
  - `async ensure(manifest) -> Path` — download-with-resume, verify, return path.
- `ModelCacheConfig` — frozen `max_retries` / `backoff_base_s` / `chunk_size`.
- `ModelFetchError`, `ModelVerificationError`, `ModelCacheError` — typed failures.

### Construction

```python
store = HttpModelStore(
    base_url="http://coordinator",   # coordinator root
    device_token="<bearer token>",   # sent as Authorization: Bearer
    client=httpx.AsyncClient(...),    # injected; caller owns its lifecycle
    cache_dir=Path("~/.fallow/models"),  # optional; expanduser applied
    config=ModelCacheConfig(),        # optional
    sleep=asyncio.sleep,              # injected clock for backoff (tests pass a fake)
)
```

## On-disk layout

```
cache_dir/<model_id>/<file_name>          verified blob
cache_dir/<model_id>/<file_name>.part     in-flight / interrupted download
cache_dir/<model_id>/<file_name>.sha256   verification marker
```

## Invariants

- A blob at its final path is only ever published via atomic `os.replace` of a
  fully-verified `.part`. A reader never sees a torn or unverified final file.
- The `.sha256` marker is written for a blob **only after** its bytes verify
  against `manifest.sha256` and `manifest.size_bytes`.
- `ensure()` fetches a given `model_id` at most once concurrently (per-model
  `asyncio.Lock`); the second caller re-checks presence under the lock.
- On sha256/size mismatch the `.part` is deleted and `ModelVerificationError`
  is raised — never retried (it is deterministic in the bytes).
- Transport failures and non-206/200 statuses are retried up to `max_retries`
  with exponential backoff, then raise `ModelFetchError`.

## Trust boundary (read this)

`path_if_present()` is on the heartbeat hot path and is called often. It does
**not** rehash the blob — rehashing a 5 GB file on every heartbeat is a
non-starter. It trusts the `.sha256` marker: if the marker exists and equals
`manifest.sha256`, the blob is considered present and valid.

Consequence: if some external process corrupts the blob bytes *after*
verification while leaving the marker intact, `path_if_present()` will still
return the path. This is an accepted trust boundary — Fallow owns `cache_dir`
and nothing else should be writing into it. (Covered by
`test_path_if_present_trusts_marker_without_rehash`.)

## Resume semantics

A `.part` may predate the current process (a previous run was interrupted). On
resume the store rehashes the existing prefix once to seed the running hash,
then sends `Range: bytes=<size>-`. If the coordinator answers `200` instead of
`206` (Range ignored), the store discards the stale prefix and restarts from
zero.

## Out of scope

- Global disk-size / eviction guard (no LRU or quota here yet).
- Blob upload / registry management (coordinator-side).
