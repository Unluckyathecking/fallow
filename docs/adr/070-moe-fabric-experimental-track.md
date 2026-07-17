# ADR 070: MoE fabric as an isolated experimental track

Status: accepted · Date: 2026-07-17

## Context

The large open Mixture-of-Experts models now appearing (Kimi K2/K3-class and
similar) carry roughly a trillion total parameters and activate tens of billions
per token. Their weights run from hundreds of gigabytes into the terabytes, so no
single school or small-business machine can hold a complete copy.

Fallow serves models by full replication: every replica is one complete model on
one machine, and the scheduler distributes whole requests (ADR 000, ADR 007).
That design has nothing to replicate onto for a model that does not fit on one
machine. Serving these models on fleet hardware splits into three problems:
getting the weights into the org without every machine downloading them, holding
them across the fleet's aggregate storage, and running interactive inference
split across machines. The first two are engineering. The third — interactive
expert-parallel inference over heterogeneous, preemptible, intermittently
available machines — is an open research problem, and ADR 000 already put
sharding out of scope until a stable wired subgroup exists.

[`docs/research/moe-fabric.md`](../research/moe-fabric.md) sets out the three
sub-problems, the research questions, and the phased sequence in full.

## Decision

Pursue MoE fabric as an isolated experimental track, separate from the
production serving path.

- The core serving path does not change. Complete-model replication (ADR 000,
  ADR 007) remains how Fallow serves the models it serves today, and the school
  pilot runs on it unchanged. MoE fabric is a subsystem alongside that path, not
  a rewrite of the coordinator, agent, scheduler, gateway, or preemption
  machinery.
- Work proceeds in the order fixed by the roadmap: fallow-modelmesh
  (peer-assisted content-addressed chunk distribution, feasible now and useful on
  its own), then a memory-pool storage benchmark, then — only if both succeed — a
  native expert-parallel MoE scheduler. Each phase has a stop condition; a phase
  does not start until the previous one answers its question.
- The interactive-inference phase is treated as research with an honest chance of
  failing on this hardware. The likely outcome is that live-token inference needs
  a stable wired compute pod while the loose fleet contributes storage and batch
  capacity only. The track is structured so that outcome stops the work cleanly
  rather than forcing a redesign of production.
- llama.cpp's RPC backend is a reference only. It is a proof of concept, it is
  insecure, and it must never run on or be exposed to the school network. Any
  experiment using it stays on an isolated bench, off the pilot fleet.

## Consequences

- The production system and the school pilot carry no risk from this work. If the
  MoE track stalls or proves unworkable, nothing that serves users is affected.
- The first phase (fallow-modelmesh) pays for itself independent of the MoE
  outcome, because peer-assisted distribution reduces uplink cost for the large
  blobs we already ship.
- We are committing to answer the open questions with measurement before building
  the scheduler, not to shipping distributed giant-model inference. It is a
  recorded possibility that interactive inference over ordinary school PCs is not
  achievable and the track ends at storage and batch contribution.
- This ADR records the decision to isolate the track. It does not approve any
  production integration; that would need its own ADR once the research phases
  report.
