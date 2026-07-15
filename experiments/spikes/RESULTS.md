# Spike results (S1)

Fill this in as spikes run on each machine. `measured-mac` = the dev MacBook,
`measured-pc` = the Windows/RTX 3070 box. Paste the raw JSON summary line under
each spike so numbers are auditable, then set the verdict.

## Summary table

| Spike | Metric | Target | measured-mac | measured-pc | Verdict |
|---|---|---|---|---|---|
| suspend_latency | total_ms p99 (no load) | < 300 ms | ~26 ms (short run) | _TBD_ | **PASS** |
| suspend_latency | total_ms p99 (`--load`) | < 300 ms | **103.1 ms** | **116.3 ms** | **PASS** |
| suspend_latency | suspend_ms p99 (raw psutil) | < 15 ms | **2.44 ms** | **0.16 ms** | **PASS** |
| cuda_suspend_cycles | failures / 500 cycles | 0 | n/a | _TBD_ | _TBD_ |
| cuda_suspend_cycles | corrupted outputs | 0 | n/a | _TBD_ | _TBD_ |
| load_times | cold ready_s (mean) | informational | _TBD_ | _TBD_ | _TBD_ |
| load_times | warm ready_s (mean) | informational | _TBD_ | _TBD_ | _TBD_ |
| load_times | post_kill ready_s (mean) | informational | _TBD_ | _TBD_ | _TBD_ |
| proxy_overhead | added TTFT p95 | < 10 ms (soft) | _TBD_ | _TBD_ | _TBD_ |

## Recorded runs (raw JSON)

2026-07-15, 100 trials, 4 children, 100ms poll, `--load` (all CPUs saturated):

```json
{"spike": "suspend_latency", "platform": "Darwin", "children": 4, "load": true, "poll_ms": 100, "trials_requested": 100, "trials_ok": 100, "verified_all": true, "total_ms_p50": 49.97, "total_ms_p95": 96.186, "total_ms_p99": 103.097, "suspend_ms_p50": 0.636, "suspend_ms_p95": 0.898, "suspend_ms_p99": 2.44, "detect_ms_p50": 48.323, "detect_ms_p99": 102.41, "target_p99_ms": 300.0, "verdict": "PASS"}
{"spike": "suspend_latency", "platform": "Windows", "children": 4, "load": true, "poll_ms": 100, "trials_requested": 100, "trials_ok": 100, "verified_all": true, "total_ms_p50": 51.736, "total_ms_p95": 109.548, "total_ms_p99": 116.279, "suspend_ms_p50": 0.121, "suspend_ms_p95": 0.13, "suspend_ms_p99": 0.159, "detect_ms_p50": 51.617, "detect_ms_p99": 116.163, "target_p99_ms": 300.0, "verdict": "PASS"}
```

**ADR-000 #3 validated on both platforms**: end-to-end yield p99 is ~110 ms under
full CPU load — 2.6× inside the 300 ms budget — and is dominated by poll phase
(detect_ms ≈ total_ms), with the suspend syscall itself sub-3 ms. The lever, if
ever needed, is poll cadence, not the suspend mechanism.

Proxy overhead (mac, short run, S1 build): added TTFT p95 ≈ 7.5 ms → PASS vs 10 ms
soft budget. cuda_suspend_cycles and load_times still require a llama-server +
model staged on the PC — scheduled with the Gate 3 two-machine demo.

## Gate-3 live two-machine demo (2026-07-15)

Fleet: coordinator + agent on MacBook Air (Apple Silicon, tailnet 100.114.3.84),
agent on Windows 11 / RTX 3070 (100.87.108.10). Model: Qwen2.5-0.5B-Instruct
Q4_K_M via llama.cpp b4589. Evidence: coordinator `events.jsonl` / `gateway.jsonl`.

- **Full pipeline**: enrollment-token registration → heartbeats → model assignment →
  agents pulled the 491MB blob from the coordinator over Tailscale (sha256-verified)
  → llama-server replicas launched (CUDA on the PC, Metal on the Mac) → READY.
- **Gateway streaming**: OpenAI-compatible SSE through the gateway, warm TTFT
  **222 ms** end-to-end (client → gateway → PC replica over tailnet), request log
  attributes model/agent/timing per request.
- **Real preemption in production**: the Mac user physically returned mid-session;
  the agent suspended its llama-server replica in **1.268 ms**
  (`{"kind":"user_returned","detail":{"yield_ms":"1.268"}}`) and auto-resumed it
  after the 120 s idle threshold. Unstaged — captured from the live event log.
- **Machine-death failover**: PC agent + replica hard-killed mid-service
  (`taskkill /F`); every subsequent request (fired immediately and through the
  45 s offline window) was served by the Mac replica — **zero failed client
  requests** across the failure.
- **Gateway defect found & fixed live**: httpx `read` timeout (15 s) fired while
  waiting for a *cold* replica's first token, making the 30 s first-byte budget
  unreachable. Fixed: transport read is now a backstop above both app-level
  guards (`gateway/config.py`), first-byte/inter-chunk enforced by `wait_for`.
- **Windows session caveat confirmed**: an SSH-launched agent lives in a network
  logon session where `GetLastInputInfo` cannot see console input (and injected
  input cannot reach it) — the documented reason deployment uses a Task Scheduler
  at-logon task in the interactive session (ADR-001, deploy/windows/).

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
