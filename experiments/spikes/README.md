# Fallow risk spikes (S1)

Throwaway-but-honest scripts that de-risk the load-bearing assumptions in
[ADR-000](../../docs/adr/000-architecture-baseline.md), especially **decision #3
(instant preemption)** and **decision #2 (coordinator proxies inference)**.

Each script is standalone (stdlib + `psutil` + `httpx` only), takes `argparse`
flags, and prints **exactly one JSON summary line** at the end so a harness can
collect results across machines. Run them with the workspace interpreter:

```bash
uv run --no-sync python experiments/spikes/<script>.py --help
```

Findings belong in [RESULTS.md](./RESULTS.md). The spike plan and what each
number decides is recorded in [ADR-008](../../docs/adr/008-spike-plan.md).

## What each spike answers

| Script | Question | Decision it informs |
|---|---|---|
| `spike_suspend_latency.py` | Input → all children `psutil`-suspended: p50/p95/**p99**? | ADR-000 #3: is the "<300ms p99, users never notice" promise real? If p99 ≥ 300ms the whole preemption thesis is wrong. |
| `spike_cuda_suspend_cycles.py` | Does suspend/resume of a live CUDA llama-server mid-generation corrupt output or wedge the process over 500 cycles? | ADR-000 #3 escalation: can a GPU replica safely sit suspended (up to `vram_evict_after_s`) before kill, or must GPU work be killed immediately? |
| `spike_load_times.py` | Cold/warm/post-kill llama-server time-to-`/health`? | ADR-000 #3 kill-for-VRAM: how expensive is a restart? Bounds scheduler thrash tolerance. |
| `spike_proxy_overhead.py` | Added TTFT of a naive httpx streaming passthrough? | ADR-000 #2: is a straight proxy gateway cheap enough, or does it need a smarter transport? |

## The critical one

`spike_suspend_latency.py` is the number the architecture stands on. It models
the real hot path: N busy child spinners (llama.cpp stand-ins), a poll thread at
`--poll-ms` (default 100, = `AgentConfig.poll_interval_ms`), and synthetic input
triggers at random phase. It reports three latencies per trial:

- `total_ms` — trigger → all children verified suspended (user-facing).
- `detect_ms` — trigger → poll thread noticed (poll-phase latency, ≤ poll-ms).
- `suspend_ms` — notice → all verified suspended (raw `psutil.suspend` cost).

Run it under contention with `--load` (extra spinners saturate every core) to
prove the poll thread still preempts fast when the machine is fully loaded.

## Platform notes / caveats

- **Suspend verification (`status`).** On macOS/Linux a suspended child reports
  `STATUS_STOPPED`; the spike polls until every child confirms it. On **Windows**
  `psutil.suspend()` calls `SuspendThread` synchronously but `status()` cannot
  report a stopped state, so verification trusts the synchronous call there. This
  is documented in-script, not hidden — treat Windows `verified_all` as "suspend
  call returned", not "status-confirmed".
- **`spike_cuda_suspend_cycles.py`** needs an already-running llama-server (it
  spawns nothing) and targets the Windows/RTX box. `nvidia-smi` sampling is
  optional; absence is reported as `null`, not an error.
- **`spike_load_times.py`** cannot enforce a page-cache drop (needs privilege),
  so it prints a per-OS hint and labels only the first `cold` rep as truly cold.
- Nothing here uses the network beyond localhost except the CUDA spike (which you
  point at your own tailnet-local server).
