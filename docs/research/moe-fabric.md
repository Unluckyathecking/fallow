# MoE Fabric: serving giant Mixture-of-Experts models on a Fallow fleet

Status: research · Date: 2026-07-17 · Related: [ADR 070](../adr/070-moe-fabric-experimental-track.md)

This is a roadmap, not a build plan. It records what we know, what we think is
tractable, and what is still an open problem, so a later decision to build (or
not build) rests on an honest reading rather than optimism.

## Why the current design does not stretch to these models

Fallow v0.1 serves models by full replication: every replica is one complete
model loaded on one machine, and the scheduler distributes whole requests across
replicas (ADR 000, ADR 007). Single-request latency does not depend on the
network, only aggregate throughput does. That design holds because the models we
serve today fit on one machine's disk and, when active, in one machine's RAM or
VRAM.

The open Mixture-of-Experts models now appearing (Kimi K2/K3-class and similar)
do not fit that assumption. They carry on the order of a trillion total
parameters and activate tens of billions per token. On disk the weights run from
hundreds of gigabytes into the terabytes. No single school or small-business
machine can hold a complete copy, so complete-model replication has nothing to
replicate onto. The property that makes these models cheap to run — only a small
fraction of the parameters is touched per token — is exactly what a
one-machine-one-replica design cannot exploit, because the whole parameter set
still has to live somewhere.

Serving one of these models on ordinary institutional hardware therefore splits
into three separate problems. Two of them are engineering. The third is a
research problem we have not solved.

## Sub-problem 1: getting the model into the org

The model has to arrive on the fleet without every machine downloading a
terabyte over the institution's uplink. A school with a single broadband line
cannot have forty machines each pull hundreds of gigabytes from an external
store.

This one we know how to do. The weights are static and can be split into
content-addressed chunks, each named by its hash, exactly as ADR 007 already
serves whole blobs by a sha256 manifest and ADR 005 keys work units by content.
A chunk fetched once by any machine on the LAN can serve every other machine on
the LAN, so the external uplink pays for roughly one copy and the internal
network distributes the rest. This is ordinary peer-assisted, content-addressed
distribution — the mechanism behind BitTorrent and behind container-image peer
distribution systems like Dragonfly and Kraken. Chunks are immutable and
hash-verified, so a peer is never trusted for correctness, only for bytes, which
fits the existing trust model where the coordinator is authority and agents
verify what they receive.

We call this component **fallow-modelmesh**. It is feasible with current
techniques and carries no unsolved research. It is the natural first step
because it is useful on its own: peer-assisted chunk distribution reduces uplink
cost for the large blobs we already ship (ADR 035's offline bundle, ordinary
model weights) whether or not the MoE inference work ever proceeds.

## Sub-problem 2: aggregate fleet storage

Once the chunks are on the LAN they have to be stored, and the fleet has to hold
a complete copy between all its machines even though no one machine can. This is
a capacity-planning and placement problem, not a research problem.

There are two tiers.

- **Durable store (SSD).** The full weight set, chunked and content-addressed,
  spread across the SSDs of the machines that opt in. Aggregate free SSD across
  even a modest fleet comfortably exceeds a terabyte. Each chunk needs a small
  replication factor so the loss of one machine does not lose a shard of the
  model; standard erasure-coding or plain N-way replication both work, and the
  placement is a bin-packing problem with known solutions.
- **Working set (RAM/VRAM).** The experts actually touched during a session have
  to be resident in memory somewhere on the fleet to be fast. This is a cache
  over the durable store, sized to the fleet's aggregate RAM and VRAM, and it is
  where sub-problem 2 hands off to sub-problem 3 — deciding *which* experts to
  hold hot, and *where*, is part of the inference problem below, not the storage
  problem.

Storage is solvable. We are not claiming it is free — replication factor, churn,
and the working-set cache all cost memory — but nothing here is unknown.

## Sub-problem 3: interactive distributed MoE inference

This is the genuinely open problem, and we should not pretend otherwise.

Full replication sidesteps distributed inference entirely: the whole model is on
one machine, so a token's forward pass never crosses the network. A model that
does not fit on one machine cannot do that. The forward pass has to be split
across machines, and for an MoE model the natural split is by expert (expert
parallelism): different machines hold different experts, and each token is routed
to the machines holding the experts its router selected.

That reintroduces exactly the thing ADR 000 chose to avoid. Autoregressive
decoding emits one token at a time, each token depends on the last, and every
token now involves a network round trip to gather the selected experts'
contributions. Fallow's fleet is the worst case for this: heterogeneous
consumer machines, on office or school Wi-Fi, that can be preempted the instant a
user touches the keyboard (ADR 002). Latency per hop is high and variable,
bandwidth is limited, and any machine can vanish mid-token. ADR 000 already
recorded that sharding is out of scope until a stable wired subgroup exists; this
document is where we take that constraint seriously rather than wishing it away.

The research directions that might make it tractable, none of them yet proven on
this class of hardware:

- **Topology-aware expert placement.** Experts that fire together on real
  traffic should sit on the same machine or on machines one hop apart. Router
  co-activation is measurable — log which experts are selected together and build
  a co-activation graph — and placement becomes a graph-partitioning problem that
  minimises cross-machine traffic. This is the highest-leverage idea here because
  it attacks the number of network hops directly rather than the cost of each
  hop.
- **Activation compression.** What crosses the network between machines is
  activation tensors, not weights. Quantising or otherwise compressing them
  trades a little accuracy for less bandwidth per hop. The question is how much
  can be shaved before quality visibly degrades.
- **Speculative decoding to hide communication.** A small model resident on the
  serving machine drafts several tokens; the large distributed model verifies
  them in one batched pass. If the draft is usually right, the fixed
  network cost is amortised over several accepted tokens instead of paid per
  token. This is the most promising lever for hiding latency, and it composes
  with the placement and compression work rather than competing with it.
- **Session-pinned KV cache.** A conversation's KV cache is large and grows with
  context. Migrating it between machines mid-session is expensive, so a session
  should pin to a stable set of machines for its lifetime, which argues for a
  distinction between machine roles (below).
- **Stable compute pods vs loose workers.** The batch fleet tolerates churn
  because a dropped work unit is simply requeued (ADR 011). Interactive MoE
  inference does not: a machine dropping mid-token stalls the whole forward pass.
  This points at a two-tier fleet — a small pool of stable, ideally wired
  machines that form a *compute pod* and carry the latency-sensitive expert
  parallelism, with the loose churning majority contributing durable storage and
  batch work but not sitting on the critical path of a live token.

The honest reading is that placement, compression, and speculation each buy
something, but none of them removes the underlying fragility of per-token network
dependence on machines that can disappear. It is plausible that interactive
giant-model inference on this fleet is only ever workable over a stable wired
subgroup, in which case the "distributed over ordinary school PCs" framing is
wrong for the interactive path and right only for storage and batch work. We do
not yet know, and the roadmap below is built to find out cheaply before
committing.

## What does not change

The core serving path stays exactly as it is. Complete-model replication (ADR
000, ADR 007) remains the way Fallow serves the models it serves today, and the
school pilot runs on it unchanged. MoE fabric is an experimental subsystem
*alongside* that path, not a replacement for it and not a rewrite of the
coordinator or the agent. Nothing in this document touches the scheduler, the
gateway, the queue, or the preemption machinery. If the MoE work stalls or
proves unworkable on this hardware, the production system is unaffected. This
isolation is the whole point of treating it as a separate track (ADR 070).

## Verdict

Bandwidth and storage are solved problems dressed in new numbers: peer-assisted
content-addressed distribution gets the weights in cheaply, and tiered
SSD-plus-memory storage holds them across the fleet. Neither needs new research.

Interactive inference of a giant MoE model across heterogeneous, intermittently
available school PCs is *not* a solved problem, and we should not tell anyone it
is. The most likely outcome is that the interactive path needs a stable wired
subgroup and the loose fleet contributes storage and batch capacity rather than
live-token compute. The work below is sequenced to establish that — or refute it
— at the lowest cost.

## Open research questions

1. On real router traffic for a K2/K3-class model, how clustered is expert
   co-activation, and does topology-aware placement cut cross-machine hops enough
   to matter?
2. How aggressively can activations be compressed before end-to-end quality
   degrades noticeably?
3. What acceptance rate does speculative decoding reach with a small on-machine
   drafter against a distributed verifier, and does it hide enough of the
   network cost?
4. What is the minimum size and network quality of a stable compute pod that
   serves one interactive session at an acceptable tokens-per-second?
5. Can a session survive the preemption or loss of a single pod machine, or does
   session-pinning make preemption fatal to the in-flight response?

## Phased sequence

Each phase has a clear question and a clear stop condition. We do not start a
phase until the previous one answers its question.

1. **fallow-modelmesh.** Build peer-assisted content-addressed chunk
   distribution over the existing manifest and blob-serving mechanism. Useful on
   its own for large-blob distribution. Stop condition: a large blob reaches a
   whole LAN at roughly one uplink copy, hash-verified end to end.
2. **Memory-pool benchmark.** Stand up the tiered durable-plus-working-set store
   and measure, on real hardware, what it costs to hold a giant model's weights
   across a fleet and to page experts into the working set. Stop condition: a
   measured storage and paging profile that tells us whether the working-set
   cache behaves well enough to build a scheduler against. This is a benchmark,
   not a product.
3. **Native MoE scheduler.** Only if the first two succeed: an expert-parallel
   scheduler with topology-aware placement from the co-activation graph, and the
   compute-pod / loose-worker split. This is where the open research questions
   get answered or the track gets stopped.

## A specific thing not to do

llama.cpp ships an RPC backend that splits a model across machines. It is
explicitly a proof of concept, and it is insecure: it trusts its peers, does no
authentication, and its own documentation warns against exposing it. It is a
useful reference for how expert/tensor splitting works and may be worth reading,
but it must never be run on the school network or exposed on any interface a
student or outside party can reach. Any experiment that uses it stays on an
isolated bench, off the pilot fleet entirely.
