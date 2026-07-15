# modelcache

The Go model cache pulls GGUF blobs from the coordinator with resume and retry,
verifies sha256 and size, and keeps verified files on local disk keyed by a cheap
marker so the heartbeat hot path never rehashes a multi-GB file.

This package is a port of the Python `fallow_agent.modelcache` package. See
[ADR 004](../../docs/adr/004-model-cache.md) for the original design rationale and
[ADR 038](../../docs/adr/038-go-supervisor-modelcache.md) for the Go-specific
decisions.

## On-disk layout (byte-compatible with the Python cache)

The Go `Store` writes exactly the same files, with the same names, as the Python
`HttpModelStore`. An operator can point either agent at the same cache directory:

```
cacheDir/<model_id>/<file_name>          verified blob
cacheDir/<model_id>/<file_name>.part     in-flight / interrupted download
cacheDir/<model_id>/<file_name>.sha256   verification marker
```

The `.part` and `.sha256` suffixes and the `<model_id>/<file_name>` blob path are
part of the on-disk contract and must not change independently in either language.
The marker file contains the hex sha256 that was verified for the sibling blob.

## Public API

```go
store := modelcache.New(baseURL, deviceToken, httpClient,
    modelcache.WithCacheDir("/var/lib/fallow/models"))

// O(1) presence check for the heartbeat hot path: trusts the marker, never
// rehashes the blob.
if path, ok := store.PathIfPresent(manifest); ok {
    // launch replica from path
}

// Verified path, downloading with resume if needed. A per-model lock collapses
// concurrent callers into a single download.
path, err := store.Ensure(ctx, manifest)
```

## Semantics

- **Marker-based presence, not rehashing.** `PathIfPresent` returns the blob only
  when the blob exists *and* the `<file>.sha256` marker matches the manifest
  digest. It never rehashes the file — rehashing a multi-GB blob on every
  heartbeat is infeasible. The accepted trust boundary is that silent
  post-verification corruption of the blob is not detected. Fallow solely owns
  the cache directory.
- **Atomic publish.** Downloads stream to `<file>.part`; only after sha256 + size
  verification does the store write the marker and `rename` the `.part` onto the
  final path. Readers never see an unverified or torn final file.
- **Range-resume with prefix rehash.** A `.part` may predate this process, so the
  store rehashes the existing prefix once to seed the hasher, then requests
  `Range: bytes=<size>-`. A `206` response appends to the prefix; a `200`
  (coordinator ignored the Range) restarts the download from zero and discards
  the stale prefix.
- **Typed, layered errors.** Transport failures and retryable (non-200/206)
  statuses are retried with exponential backoff off an injected sleep, then
  surface as `ErrFetch` (check with `errors.Is`). A content mismatch surfaces
  immediately as `ErrVerification`, deletes the `.part`, and is never retried.
- **Per-model lock.** A `sync.Mutex` per `model_id` collapses concurrent
  `Ensure` calls into a single download; the loser re-checks presence under the
  lock.

## Injected seams

`base_url`, `device_token`, the `*http.Client` (hence transport), the cache
directory, the retry `Config`, and the backoff `sleep` are all injectable, so
tests run fully in-process against an `httptest.Server` with no real network,
llama-server, or GPU.

## Out of scope

Disk-size accounting and eviction are handled by a separate component; unbounded
`cacheDir` growth is not managed here.
