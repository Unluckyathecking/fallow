# ADR 072: modelmesh peer exchange — verified chunk fetch and safe reconstruction

Status: accepted · Date: 2026-07-17 · Related: [ADR 071](071-modelmesh-core.md), [ADR 070](070-moe-fabric-experimental-track.md), [ADR 006](006-registry-auth.md)

## Context

ADR 071 built the local, verifiable core of fallow-modelmesh and left the peer
layer for a later increment. This is that increment: how a worker finds peers
holding chunks and pulls the chunks it lacks, without ever trusting a peer for
correctness.

The problem ADR 070 set out has not moved. A school on one broadband line cannot
have forty machines each pull hundreds of gigabytes over the uplink, so a blob
has to arrive roughly once and spread over the LAN. The core gave us content
addressing, a signed manifest, a local store, a delta helper, and verified
reconstruction. What was missing was the exchange itself.

## Decision

Add three small modules. The package stays standard-library only and stays a
leaf in the import DAG.

**Peer discovery.** A worker learns what its peers hold by asking each one for
its chunk availability set and folding the answers into an index from chunk hash
to the peers that hold it. That is the whole of discovery: an exchange of
availability maps, no gossip and no peer-to-peer framework. The transport is not
this package's concern. A `Peer` protocol with `available` and `fetch` is the
seam; production backs it with an HTTP client over the tailnet, which ADR 006
already authenticates, and tests back it with an in-memory fake.

**Verified chunk exchange.** Given a target manifest, a local store, and a peer
index, the exchange computes the chunks the store still needs (the delta set
from ADR 071's helper), pulls each one from a peer that holds it, and checks the
received bytes against the hash the signed manifest commits to before the chunk
enters the store. This is the security invariant of the peer layer: bytes that
do not hash to the chunk they were requested as are rejected and never stored. A
peer can lie about what it holds and can serve corrupt or hostile bytes, but it
can never place a bad chunk into a store, because every chunk is verified on
receipt against the manifest. A peer is trusted for transport, never for content.

**Resume after interruption.** Resume falls out of the delta computation, so it
needs no bookkeeping of its own. Chunks already in the store are not in the delta
set, so a fetch after an interruption asks only for what is still missing. Each
accepted chunk goes into the store as it arrives, so a fetch that drops partway
leaves the store holding what it already got, and the next call picks up from
there.

**One safe reconstruction entry point.** The core left two ways to go wrong to
the caller. The signature check could be skipped, and a failed reconstruction
could leave a half-written file that a later reader takes for a whole model. A
single entry point closes both. It verifies the manifest signature before any
bytes are written, then reconstructs to a temporary path and renames it into
place only on success, removing the temporary file on any failure. The core
`reconstruct` is unchanged, still a plain verifying reconstruction; the wrapper
adds the signature gate and the atomic write. Production code calls the wrapper.

**Coordinator stays the root of trust.** Nothing here changes what ADR 070 and
ADR 071 settled. The coordinator is the authority, and the signed manifest is how
that authority reaches a worker. A worker verifies the signature once, then
trusts any peer for chunk bytes because every chunk is checked against the
manifest. This ADR builds peer exchange but approves no production integration;
wiring it into the agent or the serving path is a separate decision with its own
ADR.

## Consequences

- The core serving path, the coordinator, and the agent are still untouched, and
  the import-linter leaf contract still fails the build if modelmesh imports them.
- Verify-on-receipt means a compromised or buggy peer degrades to slowness: a
  rejected chunk has to be fetched elsewhere, but the model is never corrupt.
- Peer selection is deliberately simple: the first holder in discovery order.
  Retrying the next holder on a rejected or failed fetch is an obvious next step,
  left until a real deployment shows it is needed.
- The `Peer` transport is a protocol with no implementation in this package. The
  HTTP-over-tailnet peer belongs with the agent wiring, behind its own ADR, so
  the leaf keeps no network code.
