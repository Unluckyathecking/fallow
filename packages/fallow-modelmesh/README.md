# fallow-modelmesh

Content-addressed model distribution primitives. This is the first modelmesh
increment described in [ADR 071](https://github.com/Unluckyathecking/fallow/blob/main/docs/adr/071-modelmesh-core.md):
the building blocks for getting a very large model onto a fleet without every
machine downloading it over the uplink.

There is no peer or network code here yet. That is a later increment. This
package is the local, verifiable core it will build on, and it depends on the
Python standard library only.

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

## Example

```python
from pathlib import Path

from fallow_modelmesh import (
    ChunkStore,
    build_manifest,
    iter_file_chunks,
    reconstruct,
    sign_manifest,
    verify_manifest,
)

key = b"shared-signing-key"
manifest = build_manifest(Path("model.gguf"), model_id="kimi-k2")
signature = sign_manifest(manifest, key)

store = ChunkStore(max_bytes=8 * 1024 * 1024 * 1024)
for chunk in iter_file_chunks(Path("model.gguf")):
    store.put(chunk)

assert verify_manifest(manifest, signature, key)
reconstruct(manifest, store, Path("restored.gguf"))
```

Fallow is pre-alpha. See the [repository README](https://github.com/Unluckyathecking/fallow#readme)
and [license](https://github.com/Unluckyathecking/fallow/blob/main/LICENSE).
