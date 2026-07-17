# ADR 074: modelmesh coordinator and agent integration — off by default, blob fallback

Status: accepted · Date: 2026-07-17 · Related: [ADR 071](071-modelmesh-core.md), [ADR 072](072-modelmesh-peer-exchange.md), [ADR 007](007-model-serving.md), [ADR 006](006-registry-auth.md)

## Context

ADR 071 and 072 built fallow-modelmesh: content-addressed chunks, a signed
manifest as the root of trust, verified peer exchange, and safe reconstruction.
Both ADRs stopped short of production. ADR 072 said wiring the mesh into the
agent or the serving path was a separate decision with its own ADR. This is that
ADR.

The constraint from ADR 070 still holds. A school on one broadband line cannot
have forty machines each drag a multi-gigabyte model over the uplink. The mesh
lets a model arrive roughly once and spread over the LAN. But the blob download
in ADR 007 works today and is what every deployment relies on, so the integration
has to add the mesh without putting a single byte of that path at risk.

## Decision

Wire the mesh into the coordinator and the agent as an additive, off-by-default
path. The blob download stays the default and stays byte-for-byte unchanged when
the mesh is off. The mesh package itself is not touched; both sides compose its
public API, and the import-linter leaf contract still fails the build if the mesh
imports the coordinator or the agent.

**Coordinator signs; it is the root of trust.** The coordinator already holds
every registered model's blob. When a shared HMAC key is configured, it serves
two read endpoints behind the existing device-token auth: a signed manifest for a
model, and one chunk by its content hash. It chunks and signs a blob once and
caches the result against the blob's size and mtime, so repeated requests do not
re-read a multi-gigabyte file, and a re-registered model with new bytes rebuilds
on its own. Without the key the endpoints are not mounted and nothing changes.

**Agent opts in; on any mesh failure it falls back to the blob.** A config flag,
off by default, swaps the agent's blob model store for one that tries the mesh
first: fetch the signed manifest, verify the signature and confirm its whole-file
hash matches the model manifest the reconcile loop already trusts, fetch the
missing chunks from peers and the coordinator with each chunk checked against the
manifest, and reconstruct atomically to the same on-disk path the blob download
writes. A bad signature, a lying peer, an unreachable coordinator, a chunk that
will not verify — every one of these falls back to the blob download. The mesh
store publishes the same verification marker as the blob store, so a model
fetched over the mesh is indistinguishable on disk and the heartbeat hot path
never knows which way it arrived.

**Delta upgrades come free.** Before fetching, the agent seeds its chunk store
from the model's existing blobs on disk. The delta helper from ADR 071 then asks
only for the chunks the new version does not share with the old one, so an upgrade
pulls the changed chunks and nothing more. Seeding is best effort: a file that
cannot be read is skipped, costing only some redundant fetching.

**Peers plug in later, behind the same seam.** This integration wires the
coordinator as the always-available chunk source. LAN peer discovery (ADR 072)
joins the same peer list ahead of the coordinator when peer addressing lands, and
is preferred because discovery order is preserved. No peer is trusted for content
either way; every chunk is checked against the signed manifest before use.

## Consequences

- The blob download is unchanged and is the only path when the mesh is off, which
  is the default. Enabling the mesh can make a fetch faster but cannot add a new
  way for it to fail, because every mesh failure falls back to the blob.
- Chunks are held in memory during reconstruction, so the store cap bounds the
  mesh path to models that fit it; a larger model simply falls back to the blob.
  A disk-backed store is the later swap when the cap starts to bite.
- The HMAC key is symmetric and shared over the enrolment channel (ADR 006), the
  same trust model as bearer tokens. An asymmetric manifest signature is the swap
  if manifests ever originate from parties an agent cannot authenticate directly.
- The signed manifest is transported as JSON but is not a wire-protocol schema
  type, so it adds no schema-drift surface.
