# ADR 077: Distributed MoE inference, design directions for the research layer

Status: proposed · Date: 2026-07-17 · Related: [ADR 070](./070-moe-fabric-experimental-track.md), [ADR 076](./076-moe-research-harness.md)

## Context

[ADR 070](./070-moe-fabric-experimental-track.md) put the MoE fabric work on an
isolated experimental track and set the rule that its open questions get answered
by measurement before anything is built. [`docs/research/moe-fabric.md`](../research/moe-fabric.md)
laid out the three sub-problems, getting the weights into the org, holding them
across the fleet, and running interactive inference split across machines, and
named the third as the one nobody has solved on this class of hardware.
[ADR 076](./076-moe-research-harness.md) stood up the bench under
`experiments/moe/` so those questions have somewhere to be answered without
leaking into production.

What none of those documents does is read the interactive-inference sub-problem
as a single design layer. The research doc lists directions; the bench measures
runners; neither says how the pieces would fit together if they worked. This ADR
is that reading. It records the design directions we would test, the tradeoffs
already visible from the outside, and the open questions each one leaves. It
closes out the design layer of the roadmap by writing the directions down in one
place so the benchmarks measure the right things.

It decides nothing about what to build. Everything below is pre-research. None of
these directions is chosen and none is scheduled. Each is a hypothesis the
harness is meant to confirm or kill against a real model, a K2/K3-class
open-weight release such as Kimi K3, before it earns a line of production code.
Writing them now is what lets the bench aim at the questions that matter rather
than the ones that are easy to measure.

Two constraints from ADR 070 hold over the whole document and are not reopened
here:

- Complete-model replication (ADR 000, ADR 007) stays the default and the only
  production serving path. The school pilot runs on it unchanged. Nothing below
  replaces it; the whole point of the isolated track is that if this work stalls,
  the system that serves users is untouched.
- llama.cpp's RPC backend is a reference to read, not a component to ship. It is
  insecure by its own documentation: it trusts its peers and does no
  authentication. It never runs on the school network or the pilot fleet, and any
  experiment that uses it stays on an isolated off-network bench (ADR 076).

## Design directions

Five directions make up the layer. The first decides whether a request should go
near the distributed path at all; the rest concern how a request that does gets
served without the per-token network dependence sinking it.

### 1. Model-hierarchy routing: pick the cheapest tier that answers the request

The distributed MoE pod is the most expensive way Fallow could answer a request
and should be the last one it reaches for. A small classifier sits in front of
the serving tiers and routes each request to the lightest one that can plausibly
handle it:

- a **small complete-model replica**, the ordinary Fallow path from ADR 000, fast
  and local, which handles most traffic;
- a **department specialist**, a mid-sized complete model replicated for one
  domain (a coding model, a model tuned for a subject area), still a single-machine
  replica, reached when the small model is likely to fall short;
- the **distributed MoE pod**, the giant expert-parallel model, reached only when
  the request genuinely needs capability the smaller tiers do not have.

The router's job is as much about refusing the pod as choosing it. Distributed
execution is frequently the wrong answer even for a hard request. A smaller model
that fits on one machine answers with no network in the loop and no pod to hold
together; when it is good enough, it wins on latency and on fragility both. For
some requests an external API is cheaper and better than standing up a pod at all,
and the honest routing decision there is to leave the fleet. The router has to
carry those exits, not just the escalation ladder, or it becomes a machine for
sending easy work to the most expensive tier.

This direction is the front door to the whole layer, and it is unproven in both
directions. We do not know whether a classifier small enough to sit in the hot
path can predict the right tier well enough to be worth having, and a wrong route
is not free: under-routing sends a hard request to a model that fails it and pays
a second time on escalation, over-routing spends pod capacity on work a local
replica would have answered. The threshold between tiers is the thing to measure,
not assume.

Open questions:

- Can a classifier cheap enough to run per request predict the right tier
  accurately enough to beat always-use-the-small-model, once the cost of wrong
  routes is counted?
- How should the router price the pod against an external API, and where is the
  request that is better served by leaving the fleet than by building a pod for
  it?
- What is the latency cost of an escalation, and does it swamp the benefit of
  routing cheap traffic away from the pod?

### 2. Stable compute pods versus loose workers

The batch fleet tolerates churn because a dropped work unit is requeued (ADR
011). Interactive expert-parallel inference does not: a machine dropping mid-token
stalls the forward pass for everyone on that pod. The direction is a two-tier
fleet. A small pool of stable, ideally wired machines forms a *compute pod* and
carries the latency-sensitive expert parallelism inside a single decoding graph.
The loose, churning majority runs independent complete-model replicas and batch
work, and contributes durable storage, but never sits on the critical path of a
live distributed token.

The tradeoff is stark and worth stating plainly. Only low-latency, stable, wired
nodes belong in a decoding graph. Putting a Wi-Fi machine that can be preempted
the instant a user touches the keyboard (ADR 002) into that graph does not add
capacity, it adds a single point of failure that fires often. Loose workers earn
their keep by doing work that survives their disappearance: a whole-model replica
whose requests can be rerouted, a batch unit that can be requeued, a shard of the
durable store that is replicated elsewhere.

The likely reading, already flagged in the research doc, is that the interactive
path is only ever workable over a stable wired subgroup, and that the "distributed
over ordinary school PCs" framing is right for storage and batch work and wrong
for live tokens. This direction is built to make that outcome cheap to accept
rather than fatal to a design.

Open questions:

- What is the minimum size and network quality of a pod that serves one
  interactive session at an acceptable tokens-per-second?
- How stable does a "stable" node have to be before it is a net gain to a pod
  rather than a liability, and how many institutions have enough such machines to
  form one at all?

### 3. Session-pinned KV cache

A conversation's KV cache is large and grows with context. Recomputing it is
expensive and moving it between machines mid-session is worse. The direction is to
pin a session to one pod for its lifetime, keep its KV cache resident on that pod,
and migrate only at a failure or a context boundary where the cache would be
rebuilt anyway. Steady-state decoding then never moves the cache; migration is the
exception, tied to an event that already forces a rebuild.

The cost of pinning is that it makes the pod a session's single point of failure
in a second way. Once a session's cache lives on a pod, losing a pod machine can
mean losing the in-flight response, not just a token. That interacts directly with
the compute-pod direction above: pinning is what makes pod stability matter, and
pod instability is what makes pinning dangerous. The two have to be measured
together.

Migration at a context boundary is the one case where moving is not pure loss:
when the context window rolls over or the session is compacted, the cache is being
rebuilt regardless, so a migration folded into that moment is close to free. The
open work is whether those boundaries come often enough to be useful handoff
points, or whether sessions mostly fail between them.

Open questions:

- Can a session survive the preemption or loss of a single pod machine, or does
  pinning make one machine's disappearance fatal to the in-flight response?
- Are context boundaries frequent enough to serve as cheap migration points, or is
  failure-time migration the only one that matters, and is rebuilding the cache
  from the durable transcript fast enough to be the fallback?

### 4. Activation compression on slow links

What crosses the network between pod machines is activation tensors, the dispatch
of a token's hidden state to the machines holding its selected experts and the
gather of their contributions back. It is not weights. Compressing or quantising
those activations trades a little accuracy for less bandwidth per hop, and on the
slow, contended links this fleet has, bandwidth per hop is a real limit.

The whole of this direction hangs on one number nobody has for this setting: how
much can be shaved before quality visibly degrades. Compress too little and the
link stays the bottleneck; compress too much and the model's output degrades in a
way a benchmark has to catch, because it will not announce itself. The answer is
almost certainly not a single ratio but a curve, and it likely depends on which
activations, at which layers, and on the model. That curve is exactly what the
`activation_compression` runner on the bench exists to trace, and it cannot be
guessed from the outside.

This direction composes with the others rather than competing. It reduces the cost
of each hop; topology-aware placement (in the research doc) reduces the number of
hops; speculative decoding (below) hides the cost that remains. None of them
removes the underlying per-token network dependence, and compression least of all,
since it makes each hop cheaper without making the model any less reliant on the
hop happening.

Open questions:

- How aggressively can dispatch and gather activations be compressed before
  end-to-end quality degrades noticeably, and is that a fixed budget or a curve
  that shifts with layer, model, and traffic?
- Does the compression and decompression cost on ordinary machines eat the
  bandwidth it saves?

### 5. Speculative decoding to hide communication latency

Autoregressive decoding emits one token at a time, and on a distributed pod each
token carries a network round trip. Speculative decoding attacks that directly: a
small model resident on the serving machine drafts several tokens, and the large
distributed model verifies them in one batched pass. When the draft is usually
right, the fixed network cost of a pod round trip is amortised over several
accepted tokens instead of paid once per token. It is the most promising lever for
hiding latency precisely because it changes how often the network is on the
critical path rather than how much each crossing costs.

The lever's value is entirely a function of the draft acceptance rate, which is
unmeasured for a small on-machine drafter against a K2/K3-class distributed
verifier. High acceptance turns one round trip into several tokens and the pod's
latency stops dominating; low acceptance means the verifier rejects the draft, the
work was wasted, and the round trip is paid anyway with drafting overhead on top.
Acceptance depends on how well the drafter tracks the large model, which is its own
research question, and the crossover point where speculation starts paying is the
thing to find.

Speculation composes with placement and compression rather than replacing them,
and it pairs naturally with the small-model tier from the routing direction: the
same small local model that can answer an easy request outright is a candidate
drafter for a hard one. Whether one model can serve both roles well is another
thing to measure, not assume.

Open questions:

- What acceptance rate does a small on-machine drafter reach against a distributed
  verifier, and does it hide enough of the network cost to matter?
- Can the small-model routing tier double as the drafter, or do the two roles want
  different models?

## How the layer fits together

Read together, the directions form a rough shape rather than an architecture. The
router keeps most traffic off the pod entirely, so the distributed path only ever
sees requests that have earned it. Those requests land on a stable pod, not the
loose fleet, and pin their KV cache there for the session. Within the pod,
placement cuts the number of hops, compression cuts the cost of each, and
speculation cuts how often the network is on the critical path at all. Each layer
attacks the per-token network dependence from a different side.

The honest reading is that none of them removes that dependence. Placement,
compression, and speculation each buy something, and stacking them buys more, but
a pod of machines that can vanish is still a pod of machines that can vanish, and
the routing tier's real job may turn out to be keeping the pod small and rarely
used rather than making it fast. That is a finding, not a failure, and the layer
is written so that it is one the benchmarks can reach cheaply.

## What this ADR does not decide

- It does not approve building any of these. Each direction is a hypothesis for
  the bench (ADR 076), not a component with a place in the system.
- It does not change the production path. Complete-model replication stays the
  default and the only thing serving users (ADR 000, ADR 007).
- It does not sanction the insecure RPC path onto any real network. That fence
  from ADR 070 stands.
- It does not fix an order of work. The phased sequence in the research doc still
  governs what gets measured when, and the interactive layer is gated behind
  fallow-modelmesh and the memory-pool benchmark succeeding first.

## Consequences

- The design layer is now written down in one place, so the runners on the bench
  can be aimed at the questions that decide each direction rather than at whatever
  is convenient to measure.
- Every direction here is falsifiable against a real open-weight model, and the
  most likely single outcome, that the interactive path needs a stable wired
  subgroup and the loose fleet contributes storage and batch capacity only, is one
  this framing accepts cleanly instead of resisting.
- Nothing in production or in the school pilot depends on any of this. If the layer
  is never built, the recorded default is unchanged.
- A decision to build any part of this needs its own ADR, taken once the relevant
  benchmark has reported. This document records the directions and their open
  questions; it does not close them.
