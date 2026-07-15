# ADR 004: Agent model cache (HttpModelStore)

Status: accepted · Date: 2026-07-15 · Module: fallow-agent/modelcache

## Context

Agents must have a model's GGUF file on local disk before launching a replica.
Files are multi-GB, office networks are flaky, and the heartbeat loop asks "do
I already have model X?" frequently. We need resumable downloads, integrity
verification, and a *cheap* presence check.

## Decision

1. **Marker-based presence, not rehashing.** After a blob verifies we write a
   sibling `<file>.sha256` marker. `path_if_present()` trusts the marker's
   existence-and-match and never rehashes the file. Rehashing 5 GB on every
   ~5 s heartbeat is infeasible. The accepted cost is a trust boundary: silent
   post-verification corruption of the blob is not detected (documented in the
   README).
2. **Atomic publish.** Downloads stream to `<file>.part`; only after sha256 +
   size verification do we write the marker and `os.replace` the `.part` onto
   the final path. Readers never see an unverified or torn final file.
3. **Resume with prefix rehash.** A `.part` may predate this process, so we
   cannot trust any in-memory hash state. On resume we rehash the existing
   prefix once to seed the hasher, then `Range: bytes=<size>-`. A `200`
   response (coordinator ignored Range) triggers a restart-from-zero.
4. **Typed, layered errors.** Transport failures and unexpected statuses are
   retried (exponential backoff off an injected `sleep`) then surface as
   `ModelFetchError`; content mismatches surface immediately as
   `ModelVerificationError` and delete the `.part` (deterministic, never
   retried).
5. **Per-model lock.** An `asyncio.Lock` per `model_id` collapses concurrent
   `ensure()` calls into a single download; the loser re-checks presence.
6. **Injected seams.** `base_url`, `device_token`, the `httpx.AsyncClient`
   (hence transport), `cache_dir`, and `sleep` are all constructor-injected, so
   tests run fully in-process via `httpx.MockTransport` with no network, no
   real llama-server, and no GPU.

## Consequences

- Presence checks are O(1) stat calls — safe on the hot path.
- Interrupted downloads resume instead of restarting, tolerating flaky links.
- A **global disk-size / eviction guard is out of scope** here; unbounded
  `cache_dir` growth must be managed by a separate component.
- The trust boundary means external corruption of a verified blob is invisible
  until launch fails; acceptable because Fallow solely owns `cache_dir`.
