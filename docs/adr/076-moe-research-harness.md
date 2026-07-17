# ADR 076: MoE research bench and its isolation

Status: accepted · Date: 2026-07-17 · Related: [ADR 070](./070-moe-fabric-experimental-track.md)

## Context

[ADR 070](./070-moe-fabric-experimental-track.md) commits the MoE fabric work to
an isolated experimental track and to answering its open questions by measurement
before building a scheduler. [`docs/research/moe-fabric.md`](../research/moe-fabric.md)
names the measurements: single-machine offload as a baseline, llama.cpp RPC as a
distribution reference, activation compression, and speculative decoding, all
compared on throughput, latency, per-token network cost, expert-cache behaviour,
and energy.

Those measurements need somewhere to live that cannot leak into production. The
llama.cpp RPC backend in particular is a proof of concept that trusts its peers
and does no authentication; ADR 070 records that it must never run on or be
exposed to the school network.

## Decision

Add a standalone research bench under `experiments/moe/`, isolated from the
serving path.

- **Typed schema, pure derivation.** A runner records a `RunObservation` (raw
  counts and timings); a pure `compute_metrics` derives the comparable
  `BenchmarkMetrics`. Every runner is measured the same way, and the derivation
  has no model or network in it, so it is unit-testable on its own.
- **Honest stubs.** Each planned benchmark is a runner that raises
  `NotImplementedError` naming what it still needs. None fakes distributed
  inference. A runner is filled in only alongside the real model or fleet it
  measures.
- **Model-free, network-free smoke test.** The schema and harness plumbing are
  tested against an injected fake observation, with no model and no network, so
  the scaffold is verifiable in CI without any of the machinery it will later
  drive.
- **Isolation.** The bench imports nothing from the coordinator, agent, or core
  serving path, and is not a workspace package — standalone scripts, no root
  `pyproject` change. It stays out of the default `pytest` run exactly as the
  spikes and fleet scaffolds do, and is run explicitly with
  `uv run pytest experiments/moe`.
- **Insecure paths stay on the bench.** Anything that runs a real model or the
  llama.cpp RPC backend belongs on an isolated bench, off the pilot fleet,
  unreachable by any student or outside party. The README carries this warning
  prominently.

## Consequences

- The production system carries no risk from the bench: it shares no code path
  with serving, and the gates that guard the packages (import-linter, mypy) do
  not span it because it imports none of them.
- The scaffold is testable now, before any model or fleet exists, which is the
  point of standing it up early.
- Filling in a runner is a deliberate, isolated act that pulls in a real model or
  fleet; it cannot happen by accident, and the insecure RPC path is fenced off in
  writing.
- This ADR records the bench and its isolation only. It approves no production
  integration; that needs its own ADR once the research phases report.
