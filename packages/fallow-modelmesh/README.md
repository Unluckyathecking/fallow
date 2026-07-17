# fallow-modelmesh

Content-addressed model distribution primitives: the building blocks for getting
a very large model onto a fleet without every machine downloading it over the
uplink.

The local core ([ADR 071](https://github.com/Unluckyathecking/fallow/blob/main/docs/adr/071-modelmesh-core.md))
chunks a model, describes it in a signed manifest, stores chunks locally, and
reconstructs the file with full hash verification. The peer layer
([ADR 072](https://github.com/Unluckyathecking/fallow/blob/main/docs/adr/072-modelmesh-peer-exchange.md))
finds which peers hold which chunks and fetches a store's missing chunks from
them, checking every chunk against the signed manifest on receipt. The transport
stays the caller's concern, so the package depends on the Python standard library
only.

## What it does

- **Chunk.** Split a model file into fixed-size, content-addressed chunks, each
  named by the sha256 of its bytes. Identical chunks, across files or across
  model versions, get the same name and are stored once.
- **Manifest.** Describe a model as an ordered list of chunk hashes plus the
  total size, chunk size, whole-file hash, and a Merkle root over the chunks.
  The manifest is the root of trust: verify it and you can trust any peer for
  bytes, because every byte is checked against a hash the manifest commits to.
- **Sign.** Produce and verify a detached HMAC-SHA256 signature over the
  manifest bytes. A tampered or unsigned manifest fails verification.
- **Store.** Hold chunks in a local, content-addressed, size-capped cache that
  dedups on write and evicts least-recently-used chunks when full.
- **Reconstruct.** Rebuild the file from a manifest and a store, re-checking
  every chunk hash and the whole-file hash and rejecting on any mismatch.
- **Delta.** Given a target manifest and a store, list the chunks the store is
  missing. Shared chunks across versions are already present, so only the new
  ones are fetched.
- **Discover.** Ask each peer for the chunks it holds and build an index from
  chunk hash to the peers that have it. Just an exchange of availability maps, no
  peer-to-peer framework.
- **Exchange.** Fetch a store's missing chunks from peers that hold them,
  checking every chunk against the signed manifest on receipt. A peer is trusted
  for bytes, never for correctness, so a tampered chunk is rejected before it is
  stored. Interrupt a fetch and the next call resumes from what the store already
  holds.
- **Reconstruct safely.** One entry point verifies the manifest signature, then
  reconstructs to a temporary file and renames it on success, so an unsigned
  manifest is never written and a failed run leaves no partial file.

## Example

```python
from pathlib import Path

from fallow_modelmesh import (
    ChunkStore,
    build_manifest,
    iter_file_chunks,
    sign_manifest,
    verified_reconstruct,
)

key = b"shared-signing-key"
manifest = build_manifest(Path("model.gguf"), model_id="kimi-k2")
signature = sign_manifest(manifest, key)

store = ChunkStore(max_bytes=8 * 1024 * 1024 * 1024)
for chunk in iter_file_chunks(Path("model.gguf")):
    store.put(chunk)

# Verifies the signature, then reconstructs atomically. An unsigned manifest
# raises before anything is written; a failed run leaves no partial file.
verified_reconstruct(manifest, signature, key, store, Path("restored.gguf"))
```

To fill a store from peers before reconstructing, discover who holds what and
fetch the delta set. Each chunk is verified against the manifest on arrival, and
a re-run after an interruption fetches only what is still missing.

```python
from fallow_modelmesh import discover, fetch_delta

index = discover(peers)  # each peer answers available() and fetch()
fetch_delta(manifest, store, index)
```

Fallow is pre-alpha. See the [repository README](https://github.com/Unluckyathecking/fallow#readme)
and [license](https://github.com/Unluckyathecking/fallow/blob/main/LICENSE).
