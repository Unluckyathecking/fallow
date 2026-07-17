# ADR 075: modelmesh offline seeding — portable bundles and verify-on-import

Status: accepted · Date: 2026-07-17 · Related: [ADR 071](071-modelmesh-core.md), [ADR 072](072-modelmesh-peer-exchange.md), [ADR 070](070-moe-fabric-experimental-track.md)

## Context

The peer layer (ADR 072) spreads a model over the LAN, but it still assumes the
first copy reaches one machine over the network. Some of the schools this is
built for have no usable uplink at all, or run the model estate on an air-gapped
segment on purpose. For them the model has to arrive on physical media: a USB
stick, an external drive, a file share carried in from somewhere with bandwidth.

The core already has everything the security story needs. A model is a signed
manifest plus a set of content-addressed chunks, and reconstruction checks every
chunk against the manifest. What was missing was a way to write that manifest and
those chunks to a directory one machine can hand to another, and to read it back
on the far side without ever trusting the medium.

## Decision

Add two small modules, `bundle` and `offline`. The package stays standard-library
only and stays a leaf in the import DAG; the import-linter contract that forbids
modelmesh from importing the coordinator, agent, or serving path still holds.

**The bundle format.** A bundle is a plain directory:

    <bundle>/
        manifest.json      the manifest's canonical bytes, exactly as signed
        signature.txt      the detached hex signature over those bytes
        chunks/<hash>      one file per distinct chunk, named by its sha256

The manifest is written as its canonical bytes, not re-serialised, so the
signature verifies byte-for-byte after the trip through the filesystem. Chunks
are named by content address, so a bundle carries each distinct chunk once even
when the model repeats it, and a chunk shared with a model already on the target
has a name the target recognises. Nothing about the format is a wire type; it is
files on a disk, which is all a USB stick can carry.

**Export.** Given a signed manifest and a store holding its chunks, export writes
the manifest, the signature, and each distinct chunk to the bundle directory. It
pulls chunks from the store the same way reconstruction does, so a machine that
can reconstruct a model can seed it.

**Import, verify before ingest.** This is where the invariant lives. Import reads
the manifest, parses it, and verifies its signature under the shared key *before
any chunk is ingested*. An unsigned or tampered manifest is rejected there, and
the store is untouched. Then, for each chunk the store still lacks, import reads
the file, re-hashes it, and checks it against the hash the signed manifest commits
to; a chunk that does not match is rejected before it can enter the store. A
bundle is trusted for transport and nothing else. It can be corrupted on a failing
drive or rewritten by someone hostile, and neither a bad manifest nor a bad chunk
can get into a store. This is the same rule the peer layer enforces on receipt,
applied to bytes off a disk instead of off the network.

**Partial and resumable import.** Resume falls out of the delta set, exactly as it
does for peer fetch, with no bookkeeping of its own. Import asks only for the
chunks the store does not already hold, so a model that shares chunks with one
already present pulls only the new ones, and an import interrupted partway leaves
the store holding what it got and skips those on the next run. A chunk already in
the store is not re-read from the bundle, so its file need not even be present.

**Reconstruction is unchanged.** After a verified import the store holds the
model's chunks, and the caller reconstructs through the existing signature-gated,
atomic entry point with no network involved. Offline seeding adds a way to fill
the store; it changes nothing about how a filled store becomes a file.

## Consequences

- The core, the peer layer, the coordinator, and the agent are untouched, and the
  leaf contract still fails the build if modelmesh reaches into the serving path.
- A bundle is inert and self-describing: anyone can carry it, and only the holder
  of the signing key can produce one an import will accept. The medium is never
  trusted, so a bundle needs no secure channel to travel over.
- Parsing a manifest off untrusted media is a boundary, so the parse validates
  every field's shape before building the value and rejects malformed JSON, rather
  than letting a bad file surface as an unrelated error deeper in.
- Export writes a directory, not an archive. Zipping or splitting a bundle across
  several sticks is a packaging concern for whatever carries it, left out of the
  library until a real deployment asks for it.
- This ADR builds offline seeding but approves no production integration. Wiring
  export and import into the agent or an operator tool is a separate decision with
  its own ADR.
