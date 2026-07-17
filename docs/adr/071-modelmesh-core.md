# ADR 071: modelmesh core — content-addressed chunks and a signed manifest

Status: accepted · Date: 2026-07-17 · Related: [ADR 070](070-moe-fabric-experimental-track.md), [ADR 007](007-model-serving.md), [ADR 005](005-queue-store.md)

## Context

ADR 070 committed to the MoE fabric as an isolated experimental track and fixed
the order of work: fallow-modelmesh first, because peer-assisted
content-addressed distribution is feasible now and pays for itself on the large
blobs Fallow already ships, regardless of whether the later MoE inference work
proceeds. [`docs/research/moe-fabric.md`](../research/moe-fabric.md) sets out
why: a school on one broadband line cannot have forty machines each pull
hundreds of gigabytes, so a blob has to arrive roughly once and spread over the
LAN.

This ADR records the first increment: the local, verifiable core, with no peer
or network code yet. The peer layer is a later increment and will build on
these primitives. The package is a leaf in the import DAG. It must not import
the coordinator, the agent, the gateway, or the protocol package, and it must
not touch the core serving path. That isolation is the whole point of ADR 070,
enforced here by an import-linter contract.

## Decision

Build a standard-library-only package with five parts.

**Content addressing.** A model file is split into chunks, each named by the
sha256 hex digest of its bytes. This is the same content-addressing Fallow
already uses: ADR 007 serves whole blobs by a sha256 manifest, and ADR 005 keys
work units by content. A chunk fetched once by any machine can serve every
other machine, and a peer is trusted for bytes only, never for correctness,
because every byte is checked against a hash. Identical chunks are stored once,
which dedups shared regions across model versions for free.

**Chunk size.** Fixed-size chunks of 4 MiB. Fixed-size chunking is the simplest
thing that dedups identical regions landing on the same boundaries, which fits
static, aligned weight files. 4 MiB keeps the manifest small for terabyte-scale
weights (a 1 TB model is about 250k chunk hashes, a few megabytes of manifest)
while staying a sensible peer-transfer and dedup unit. The coordinator's HTTP
blob route uses a 1 MiB read size for streaming (ADR 007); that is a transport
detail and independent of this dedup unit. If real cross-version dedup proves
poor because edits shift content off the boundaries, content-defined chunking
(a rolling hash that cuts on content) is the later swap, at the cost of more
code. We do not pay for it until measurement says we need it.

**Merkle manifest.** The manifest lists the chunk hashes in order plus the total
size, chunk size, whole-file sha256, and a Merkle root over the chunk list. The
Merkle root is a single hash that commits to every chunk and to their order, so
one signature over the manifest vouches for the whole list. Leaves and internal
nodes are domain-separated with a one-byte prefix so a leaf can never be
replayed as an internal node. The root also gives the later peer layer a compact
commitment for verifying chunks as they arrive, without waiting for the whole
file. The manifest is a frozen value and deliberately not a wire type: it
serialises to canonical JSON for signing, but it is not part of the
coordinator/agent protocol schema, so it adds no schema-drift surface.

**Signing.** A detached HMAC-SHA256 signature over the manifest's canonical
bytes. This reuses the repo's existing pattern (`hmac` plus a constant-time
compare, as in the registry's bearer tokens) and adds no dependency. HMAC is
symmetric, which fits the current trust model: the coordinator is the sole
authority and hands the shared verification key to agents over the already
authenticated enrolment channel (ADR 006). Detached keeps the signed bytes
identical to what was hashed. When manifests one day originate from a party an
agent cannot authenticate directly, this swaps for an asymmetric signature
(ed25519 via pyca cryptography) behind the same two functions, without touching
callers. We do not add that dependency now because nothing yet needs it.

**Coordinator as root of trust (for later).** The peer layer is not built here,
but the design assumes what ADR 070 already states: the coordinator is
authority and agents verify what they receive. The signed manifest is how that
authority reaches an agent. An agent verifies the signature once, then trusts
any peer for chunk bytes because each chunk is checked against the manifest. No
peer is ever trusted for correctness. This ADR does not build peer exchange and
does not approve any production integration; that needs its own ADR once the
peer increment lands.

**Local chunk store and reconstruction.** An in-memory, content-addressed cache
with put/get/has, an availability set, and least-recently-used eviction under a
byte cap. It dedups on write. Reconstruction rebuilds the file from a manifest
and a store, re-hashing every chunk as it reads and checking the whole-file hash
at the end, and rejects on any chunk-hash or size mismatch. The store guarantees
a chunk matches its key on write, but reconstruction re-checks anyway, because a
later disk- or peer-backed store can return rotted bytes and this is the layer
that must catch it. A delta helper reports the chunks a store is missing for a
target manifest, which is the fetch plan the peer layer will act on.

## Consequences

- The core serving path, the coordinator, and the agent are untouched. The
  import-linter contract fails the build if modelmesh ever imports them.
- The package is useful on its own for large-blob distribution once the peer
  layer exists, independent of the MoE inference outcome, exactly as ADR 070
  intended.
- Two choices are explicitly deferred behind stable interfaces: content-defined
  chunking (if fixed-size dedup underperforms) and asymmetric signing (if
  manifests come from parties agents cannot authenticate). Neither is built now,
  and neither forces a caller change when it is.
- The in-memory store is a starting point. A disk-backed store for holding a
  model's weights across a fleet is the memory-pool benchmark that ADR 070
  sequences next, not part of this increment.
