# The Fallow scheduling experiment

This is the research protocol for the wave-4 scheduling study. It defines the
question, the three arms, exactly how a run is configured and driven, and how each
headline metric is computed from the logs the run emits.

> **Status.** The coordinator, agents, all three scheduler arms, and the audit logs
> described here are **built** (the churn-aware arm is config-selectable, see §2). The
> `fallow-bench` harness — workload generator (B1, [ADR 019](adr/019-bench-workload.md)),
> churn injector (B2, [ADR 020](adr/020-bench-churn.md)), and metrics analysis (B3,
> [ADR 021](adr/021-bench-analysis.md)) — is being assembled in wave 4. The `churn`
> subcommand is wired today; the `run` and `analyze` subcommands expose the B1 and B3
> libraries and are landing alongside them. The commands below are the harness's stable
> interface; where a piece is still being wired that is stated plainly.

---

## 1. Question

> Given a fleet of shared desktops that people are actively using, how much useful
> private inference and batch compute can Fallow harvest **without the users noticing**,
> and does a churn-aware scheduler beat naïve baselines on the metrics that matter?

The study is comparative: it pits schedulers against each other under an **identical,
replayable** workload and churn trace, and reports the trade-offs.

---

## 2. The three arms

An **arm** is a `(fleet configuration, scheduler policy)` pair, selected by the
coordinator's `scheduler` config field
(`CoordinatorConfig.scheduler: "capability" | "roundrobin" | "churn_v2"`,
`app/config.py`; `_build_policy` in `app/factory.py` constructs the policy). The
workload and churn trace are held identical across arms; only these two things change.

| Arm | `scheduler` | Fleet | Policy | What it answers |
| --- | --- | --- | --- | --- |
| **(a)** `dedicated` | `capability` | one always-on machine, **no churn** | `CapabilityScheduler` over a fleet of one | "What would buying a single dedicated box get you?" — the throughput ceiling / latency floor. |
| **(b)** `round_robin` | `roundrobin` | full churning fleet | `RoundRobinScheduler` | Capability-**blind** fair rotation: the naïve distributed baseline. |
| **(c)** `churn_v2` | `churn_v2` | full churning fleet | `ChurnAwareScheduler` | Does ranking placement by an empirical idle-survival model beat round-robin under churn? |

All three policies are pure implementations of `fallow_protocol.interfaces.SchedulerPolicy`
(`scheduler/policies.py`, `scheduler/v2.py`): identical inputs give identical outputs, so an
arm is a hot-swap and a run is reproducible.

- **`CapabilityScheduler`** (the v1 default, and arm (a)'s policy) filters to eligible
  agents (`IDLE`, not `suspect`, GPU-capable when required) then ranks by (1) already holds
  a warm READY/SUSPENDED replica of the model, (2) has a GPU, (3) most free RAM, with
  `agent_id` as the deterministic tiebreak.
- **`RoundRobinScheduler`** carries one integer cursor, ignores every capability signal,
  applies only the eligibility gate, and hands work out in a fair rotation over the
  `agent_id`-sorted eligible set. `reset()` restores the deterministic start.
- **`ChurnAwareScheduler`** ranks eligible agents by an empirical **idle-survival model**
  (`ChurnModel`, `scheduler/churn_model.py`) built once at startup from the coordinator's
  `events.jsonl`, bucketed by hour-of-day read through the injected clock — it prefers the
  agent least likely to have its user return before an estimated
  `churn_est_unit_duration_s` completes, minimising wasted, requeued work. A missing/empty
  event log falls back to an optimistic prior. Live model refresh is deferred
  ([ADR 022](adr/README.md), future work).

---

## 3. How to run

### 3.1 Coordinator config per arm

Each arm is one coordinator process configured from a TOML file overlaid with
`FALLOW_COORD_*` env vars (`app/config.py`). Set the arm and fix the storage/audit paths
so the run is self-contained and the logs are the dataset:

```toml
# arm-c.toml
scheduler                 = "churn_v2"   # "capability" (arm a) | "roundrobin" (arm b)
churn_est_unit_duration_s = 30.0         # v2 survival horizon
db_path                   = "runs/churn_v2/fallow.db"
blob_dir                  = "runs/churn_v2/blobs"
unit_input_dir            = "runs/churn_v2/units"
events_jsonl_path         = "runs/churn_v2/events.jsonl"
gateway_log_path          = "runs/churn_v2/gateway.jsonl"
admin_key                 = "..."        # or FALLOW_COORD_ADMIN_KEY
host                      = "100.x.y.z"  # coordinator tailnet IP
```

Arm **(a) `dedicated`** uses a single always-on agent and no churn injector. Arms **(b)**
and **(c)** use the full fleet plus the churn injector and differ only in `scheduler`.

### 3.2 `bench_mode` agents

Set `bench_mode = true` in the agent config (`AgentConfig.bench_mode`). This enables the
agent's `/debug/simulate_input` endpoint so the churn injector can drive **synthetic
user-return taps deterministically** from the seeded trace instead of waiting for a real
person. `bench_mode` MUST be off in any real deployment.

### 3.3 Harness commands

```bash
# 1. replay a seeded interactive workload against an arm → client_trace.jsonl
#    Open-loop: requests fire at precomputed offsets and never wait for prior ones
#    (a closed loop would throttle a slow arm and hide the effect being measured).
python -m fallow_bench run    --config workloads/mixed.yaml --out runs/churn_v2

# 2. replay the seeded fleet-churn schedule via /debug/simulate_input → churn.jsonl
#    (the ONLY wired subcommand today; the injector owns the only real clock/HTTP)
python -m fallow_bench churn  --config traces/office_day.yaml --out runs/churn_v2

# 3. reduce the run's logs into the cross-arm headline table
python -m fallow_bench analyze runs/churn_v2 --baseline runs/dedicated
```

### 3.4 Seeds and determinism

Per the project's hard rules, the harness uses **injected clocks and seeds only**: no
wall-clock reads, no unseeded randomness in logic. That makes `analyze` reproducible on
the same inputs. Specifically:

- **Workload (B1):** inter-arrival gaps are drawn from `random.Random(seed)` and the full
  `(t_offset_s, prompt_idx, max_tokens)` list is fixed **before** the run; identical seeds
  give byte-identical schedules across arms.
- **Churn (B2):** every value is drawn from one `numpy.random.default_rng(seed)`; each
  agent is an independent idle→active renewal process (lognormal idle gaps and session
  durations) emitting user-return taps; `agent_kill` / `net_drop` are opt-in low-rate
  Poisson extras drawn after the returns. A scripted schedule can override the generator
  verbatim for hand-authored scenarios. The injector owns no time — clock and `sleep` are
  injected.
- **Analysis (B3):** no wall-clock and no randomness; percentiles use a single `numpy`
  linear-interpolation definition across every latency metric.

Report every seed alongside every table.

---

## 4. Metric definitions

B3 reduces up to **six per-arm logs** into one cross-arm table (`AnalysisConfig` names
them; a missing file degrades that metric to an em dash `—` rather than crashing). Every
metric is a pure function `frame(s) → float | None`. `p50/p95/p99` are reported for
latency rows via the shared numpy linear-interpolation percentile.

| Metric | Definition | Source log |
| --- | --- | --- |
| **TTFT** | `t_first_token − t_submit` per request | `client_trace.jsonl` (B1) |
| **tok/s** (decode) | `tokens_out ÷ (t_done − t_first_token)` per request | `client_trace.jsonl` (B1) |
| **units/hour** | distinct `work_unit_id`s in state `done` ÷ elapsed hours of the log | `job_status.jsonl` |
| **recovery time** | wall time from an agent/replica death to its work being requeued and re-served | `events.jsonl` (+ requeue/next served) |
| **time-to-yield** | `yield_ms` on every `user_returned` event | `events.jsonl` (`detail.yield_ms`) |
| **energy** | marginal draw, idle baseline subtracted (see §4.1) | `power.jsonl` |
| **% served on-prem** | `100 × served ÷ (served + shed + error)` | `gateway.jsonl` (`status`) |

`% served on-prem` reads only the `status` field of each `GatewayLogEntry`, using the
exact `LogStatus` values the gateway records: `served` (a replica produced the bytes),
`shed` (503, no replica available), `error` (502, no replica reachable after one retry).
Interactive TTFT and tok/s come from the **client trace** (the bench client records
`tokens_out` and its own `t_first_token`/`t_done`), not from `gateway.jsonl`, which
deliberately logs only routing/timing/audit fields.

### 4.1 Marginal-energy methodology

Energy is reported as **marginal** draw, not total — the additional energy Fallow causes
over what the machine would draw anyway. B3 implements exactly this: an `EnergyBaseline`
window `[start_s, end_s]` (in the power log's own time units) defines each agent's idle
mean draw, which is **subtracted before integrating** `power.jsonl` over the run. Because
Fallow only runs while a user is away, marginal energy also excludes the machine's fixed
"on anyway" draw by construction. Normalise to **Wh per 1000 tokens** and **Wh per
completed batch unit** for cross-arm comparison. Prefer a wall-socket meter feeding
`power.jsonl`; fall back to a software estimate (RAPL / `nvidia-smi`) elsewhere — see the
validity threat below.

---

## 5. Threats to validity

Stated up front so results are read honestly:

- **Model size (0.5B).** The demo fleet runs Qwen2.5-0.5B-Instruct Q4_K_M. Load times,
  VRAM footprint, tok/s and the cost of a cold model load all scale with model size, so
  absolute numbers do not transfer to a 7B/70B fleet. The *comparative* arm results are
  more robust than the absolute figures.
- **Two-machine fleet.** Validation used one Mac + one RTX box. Scheduler behaviour that
  only manifests at fleet scale (contention, tail effects, the single-writer SQLite
  coordinator as a bottleneck) is under-sampled, and the round-robin vs churn-aware gap
  generally widens with fleet size and heterogeneity — so a 2-machine result understates
  it.
- **Synthetic churn model.** User presence is a seeded idle→active renewal process driven
  through `/debug/simulate_input`, not real people. It is deterministic and repeatable
  (good for comparing arms) but is a *model* of office churn; results are only as
  representative as the trace and its lognormal parameters.
- **Software-only power on some boxes.** Where no wall-socket meter feeds `power.jsonl`,
  energy is a software estimate (RAPL / `nvidia-smi`) that misses PSU losses, non-CPU/GPU
  draw and platform overhead. Treat cross-machine energy comparisons that mix metered and
  estimated boxes as indicative, not precise.

---

## 6. References

- Architecture as built: [`docs/architecture.md`](architecture.md)
- Scheduler decisions: [ADR 011](adr/011-scheduler-v1.md) (v1 arms), churn-aware v2 arm
  ([ADR 022](adr/README.md)); gateway: [ADR 012](adr/012-gateway.md)
- Bench harness: workload [ADR 019](adr/019-bench-workload.md), churn
  [ADR 020](adr/020-bench-churn.md), analysis [ADR 021](adr/021-bench-analysis.md)
- Preemption validation and raw numbers: [`experiments/spikes/RESULTS.md`](../experiments/spikes/RESULTS.md)
- Integration/chaos suite exercising churn, eviction and failover:
  [ADR 016](adr/016-integration-suite.md)
