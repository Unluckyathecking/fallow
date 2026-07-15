# ADR 008: Risk-spike plan (S1)

Status: accepted · Date: 2026-07-15

## Context

ADR-000 makes two load-bearing bets that, if wrong, invalidate the architecture:
decision #3 (instant `psutil.suspend()` preemption within 300ms p99, escalating
to kill for VRAM) and decision #2 (the coordinator proxies every interactive
request to a replica port). Both are assumptions, not measurements. Before any
module is built on them, we need real numbers on the target hardware (a dev
MacBook and a Windows/RTX 3070 box) — cheaply, and honestly.

## Decision

Ship four standalone, throwaway spike scripts under `experiments/spikes/`
(stdlib + `psutil` + `httpx` only), each printing one JSON summary line:

1. **`spike_suspend_latency.py`** — the critical number. Busy child spinners +
   a 100ms poll thread + random-phase input triggers; report total / detect /
   suspend latency p50/p95/p99, with a `--load` stress mode. Target: total p99
   < 300ms; raw suspend p99 < 15ms.
2. **`spike_cuda_suspend_cycles.py`** — suspend/resume a live CUDA llama-server
   mid-generation for 500 cycles; count failures, error kinds, and temperature-0
   first-token corruption. Validates that a suspended GPU replica survives until
   the kill escalation.
3. **`spike_load_times.py`** — llama-server time-to-`/health` for cold / warm /
   post-kill launches; bounds the cost of the kill-for-VRAM restart path.
4. **`spike_proxy_overhead.py`** — added TTFT of a naive httpx streaming
   passthrough vs direct, using a localhost SSE stub (no llama-server needed).

Results and per-OS run instructions live in `experiments/spikes/RESULTS.md`.
These scripts are excluded from typing/packaging (`experiments/**` is ANN-ignored
in ruff and outside the mypy packages); they must stay runnable, not shippable.

## Consequences

- We learn whether ADR-000 #3 survives contact with real hardware before writing
  the `Preemptor`/`ProcessSupervisor` modules; a p99 ≥ 300ms forces a redesign
  (RT thread / OS input signal instead of polling).
- The CUDA spike tells us whether `vram_evict_after_s` suspend-then-kill is safe
  or GPU work must be killed on sight — a scheduler policy input.
- Windows suspend verification is `status()`-blind (documented caveat); Windows
  `verified_all` means "suspend call returned", so we trust `total_ms` there.
- Throwaway code: no tests, no ADR-per-module discipline beyond this plan.
