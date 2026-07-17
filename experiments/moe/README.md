# MoE research bench (Track C)

> **EXPERIMENTAL — isolated bench only. Do not run on the school network or the
> production fleet.**
>
> This is a research bench for the MoE fabric track ([ADR 070](../../docs/adr/070-moe-fabric-experimental-track.md),
> [ADR 076](../../docs/adr/076-moe-research-harness.md)). It is a proof-of-concept
> scaffold that will grow to drive **insecure** components — in particular
> llama.cpp's RPC backend, which trusts its peers, does no authentication, and is
> documented as unsafe to expose. Anything here that runs a real model or splits
> one across machines belongs on an isolated bench, off the pilot fleet entirely,
> unreachable by any student or outside party. Nothing in this directory imports
> or touches the coordinator, agent, or the core serving path, and it must never
> be wired into them.

## What this is

A skeleton for the benchmarks the MoE roadmap sequences ([`docs/research/moe-fabric.md`](../../docs/research/moe-fabric.md)):
a typed metrics schema and the harness plumbing that turns a runner's raw
observation into comparable metrics. The runners for the real benchmarks are
stubs that raise `NotImplementedError` — they name what they measure and what
they still need, and none of them fakes distributed inference.

This is a bench, not a product. It answers questions cheaply; it does not serve
anyone.

## Layout

* `metrics.py` — `RunObservation` (raw counters a runner records) and
  `BenchmarkMetrics` (the derived, comparable numbers), plus `compute_metrics`,
  the pure derivation with no model or network in it.
* `harness.py` — the `Runner` protocol, `BenchmarkConfig`, and `run_benchmark`,
  which runs an injected runner and derives its metrics.
* `runners.py` — one stub per planned benchmark, each raising
  `NotImplementedError` until a real model or fleet is wired up.
* `tests/test_harness.py` — smoke test: exercises the schema and plumbing against
  an injected fake, with no model and no network.

## Metrics

`BenchmarkMetrics` is the comparison surface across every runner:

| Field | Meaning |
|---|---|
| `tokens_per_sec` | Decode throughput over the run. |
| `time_to_first_token_ms` | Latency from request to first token. |
| `per_token_network_bytes` | Cross-machine bytes per generated token — the cost expert parallelism adds. |
| `expert_cache_hit_rate` | Fraction of expert lookups already resident in the working set. |
| `watt_hours_per_million_tokens` | Energy per million generated tokens. |

## Planned benchmarks (all stubs)

* `single_machine_offload` — one machine paging experts across VRAM/RAM/SSD; the
  baseline everything distributed is measured against.
* `llama_cpp_rpc` — llama.cpp RPC expert/tensor split as a reference point.
  **Insecure; isolated off-network bench only.**
* `activation_compression` — how far cross-machine activations compress before
  quality degrades.
* `speculative_decoding` — an on-machine drafter verified by the distributed
  model, to hide per-token network cost.

## Running the smoke test

From the repository root:

```bash
uv run pytest experiments/moe
```

The default `uv run pytest` (which collects `packages` and `tests`) does not
descend here, matching how the spikes and fleet scaffolds stay out of the main
suite. Run the command above to exercise this bench.
