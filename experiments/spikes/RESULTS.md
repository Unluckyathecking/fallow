# Spike results (S1)

Fill this in as spikes run on each machine. `measured-mac` = the dev MacBook,
`measured-pc` = the Windows/RTX 3070 box. Paste the raw JSON summary line under
each spike so numbers are auditable, then set the verdict.

## Summary table

| Spike | Metric | Target | measured-mac | measured-pc | Verdict |
|---|---|---|---|---|---|
| suspend_latency | total_ms p99 (no load) | < 300 ms | _TBD_ | _TBD_ | _TBD_ |
| suspend_latency | total_ms p99 (`--load`) | < 300 ms | _TBD_ | _TBD_ | _TBD_ |
| suspend_latency | suspend_ms p99 (raw psutil) | < 15 ms | _TBD_ | _TBD_ | _TBD_ |
| cuda_suspend_cycles | failures / 500 cycles | 0 | n/a | _TBD_ | _TBD_ |
| cuda_suspend_cycles | corrupted outputs | 0 | n/a | _TBD_ | _TBD_ |
| load_times | cold ready_s (mean) | informational | _TBD_ | _TBD_ | _TBD_ |
| load_times | warm ready_s (mean) | informational | _TBD_ | _TBD_ | _TBD_ |
| load_times | post_kill ready_s (mean) | informational | _TBD_ | _TBD_ | _TBD_ |
| proxy_overhead | added TTFT p95 | < 10 ms (soft) | _TBD_ | _TBD_ | _TBD_ |

## Run instructions per spike

Prefix everything with the workspace interpreter from the repo root:
`uv run --no-sync python experiments/spikes/<script>.py ...`

### 1. suspend_latency (run on BOTH mac and pc)

```bash
# baseline
uv run --no-sync python experiments/spikes/spike_suspend_latency.py \
    --children 4 --trials 100 --poll-ms 100 --seed 1
# under full CPU load (the honest stress case)
uv run --no-sync python experiments/spikes/spike_suspend_latency.py \
    --children 4 --trials 100 --poll-ms 100 --load --seed 1
```

- macOS: no special privileges needed.
- Windows: run from a normal PowerShell; `verified_all` means "suspend call
  returned" (see README caveat), so cross-check `total_ms_p99` against target.

### 2. cuda_suspend_cycles (Windows/RTX box only)

Start a llama-server first, note its URL and PID, then:

```powershell
# find the PID, e.g. via Get-Process llama-server
uv run --no-sync python experiments/spikes/spike_cuda_suspend_cycles.py `
    --server-url http://127.0.0.1:8080 --pid <PID> --cycles 500
```

- Requires the llama.cpp native `/completion` SSE API.
- `nvidia-smi` on PATH enables VRAM before/after sampling (optional).

### 3. load_times (run on BOTH; needs a real binary + model)

```bash
uv run --no-sync python experiments/spikes/spike_load_times.py \
    --binary /path/to/llama-server --model /path/to/model.gguf --port 8090 --reps 3
```

- macOS truly-cold rep: run `sudo purge` before, as the printed hint says.
- Linux truly-cold rep: `sync && echo 3 | sudo tee /proc/sys/vm/drop_caches`.
- Windows truly-cold rep: clear the standby list (RAMMap / EmptyStandbyList).
- Pass GPU flags after `--extra`, e.g. `--extra --n-gpu-layers 999`.

### 4. proxy_overhead (run on BOTH; no llama-server needed)

```bash
uv run --no-sync python experiments/spikes/spike_proxy_overhead.py \
    --reps 100 --chunks 8 --first-delay-ms 40 --inter-delay-ms 10
```

- Pure localhost; isolates gateway passthrough cost via paired direct-vs-proxy
  measurements so origin jitter cancels.

## Interpretation

- If `suspend_latency total_ms p99 < 300ms` holds under `--load` on both boxes,
  ADR-000 #3 is validated. If not, the preemption design must change (dedicated
  RT thread, faster poll, or OS-level signal instead of polling).
- If `cuda_suspend_cycles` shows any corruption/wedge, GPU replicas must be
  killed on user return rather than suspended — revisit `vram_evict_after_s`.
- `load_times` sets the cost of the kill/restart escalation; large cold times
  argue for suspend-first, kill-last scheduling.
- `proxy_overhead` within budget ⇒ a naive httpx passthrough gateway is fine for
  v0.1.
