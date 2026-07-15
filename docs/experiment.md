# The Fallow scheduling experiment

This is the research protocol for the wave-4 scheduling study. It defines the
question, the three arms, exactly how a run is configured and driven, and how each
headline metric is computed from the logs the run emits.

> **Status.** The coordinator, agents, scheduler arms, workload driver, churn injector,
> analysis pipeline, and canonical experiment runner are implemented. CI exercises a
> short smoke scenario with an in-process coordinator and fake replicas. The full
> 18-hour study still requires the physical fleet and remains an operator-run activity.

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
  (`ChurnModel`, `scheduler/churn_model.py`) built once at startup from the immutable file
  passed through `--churn-history`. The run's `events.jsonl` remains output-only. Sessions
  are bucketed by hour of day through the injected clock, and the policy prefers the agent
  least likely to have its user return before an estimated
  `churn_est_unit_duration_s` completes, minimising wasted, requeued work. Live model
  refresh is deferred
  ([ADR 022](adr/README.md), future work).

---

## 3. How to run

### 3.1 Prerequisites

Before a live run, check that clocks are synchronized, every fleet host is reachable,
the required models are assigned and ready, and the output root is new and writable.
Set `FLW_ADMIN_KEY` to the key the run coordinators should use. The runner mints a fresh
client API key for each run. Coordinator templates do not store the admin key on disk.
Live energy results also need working power telemetry.

Prepare two clean, checkpointed coordinator databases. The file passed through
`--dedicated-seed-db` contains only the dedicated agent. The file passed through
`--seed-db` contains the full distributed fleet used by both other arms. Both databases
contain the model catalogue and assignments but no jobs or work units. The runner copies
the appropriate file into each run, so device tokens remain valid without sharing mutable
state across arms. Do not copy a database while its coordinator is running.

Pass an immutable historical event log through `--churn-history`. The churn-aware policy
fits its idle-survival model from this input at startup. The run's new `events.jsonl`
remains output-only, so historical samples do not contaminate the measured dataset.

The dedicated arm is an operator-controlled fleet boundary. Its template selects the
`capability` scheduler, but configuration alone cannot guarantee that only the intended
always-on machine is enrolled. Confirm fleet membership before starting that arm.

For churn runs, enable the agent bench listener with `[bench] enabled = true`. Bind this
unauthenticated test listener only to loopback or a trusted tailnet address, and disable
it after the experiment.

Headless Linux experiment hosts may also set `force_idle = true` in the same table. The
agent rejects forced idle when bench mode is disabled and logs a warning when it is active.
Never use that setting on a machine used by a person. The provider-neutral preparation and
secret-handling steps live in [`experiments/fleet/README.md`](../experiments/fleet/README.md).

### 3.2 Canonical plan

The runner defines nine runs in a stable order: three `dedicated` repetitions, three
`round_robin` repetitions, then three `churn_v2` repetitions. Repetitions 1, 2, and 3
use seeds 17, 29, and 43 respectively. The same seed is paired across arms so the
scheduler is the intended variable. Live runs last 7,200 seconds. Smoke runs retain the
same plan and seeds but use a 120-second duration.

The coordinator templates live in `experiments/arms/`. They select the exact scheduler
for each arm and render the database, blob, unit input, result, event, and gateway paths
inside that run's directory.

### 3.3 Commands

Run the full live plan:

```bash
FLW_ADMIN_KEY=... python -m fallow_bench experiment \
  --config experiments/main.yaml \
  --dedicated-seed-db experiments/seed-dedicated.db \
  --seed-db experiments/seed-fleet.db \
  --churn-history experiments/churn-history.jsonl \
  --host 100.x.y.z \
  --revision "$(git rev-parse HEAD)" \
  --out experiments/runs
```

Use `--smoke` for the 120-second plan. A failed or interrupted run can be narrowed with
`--arm dedicated|round_robin|churn_v2` and `--repetition 1|2|3`. Filters can be combined.
The runner refuses to reuse an existing `<root>/<arm>/rep-XX` directory.

The lower-level commands remain available when one phase needs to be exercised alone:

```bash
python -m fallow_bench run --config experiments/main.yaml --out runs/standalone
python -m fallow_bench churn --config experiments/main.yaml --out runs/standalone
```

Each run writes metadata and initializes its canonical logs before activity. It captures
the idle-energy baseline next, then runs workload and churn concurrently. The dedicated
arm skips churn and records an empty `churn.jsonl`. A phase failure cancels its sibling
and runs cleanup before the error is returned.

### 3.4 Run directory contract

Each run lives at `<root>/<arm>/rep-XX/` and contains:

```text
coordinator.toml  run_meta.json     client_trace.jsonl
gateway.jsonl     events.jsonl      churn.jsonl
power.jsonl       units.jsonl       schedule.jsonl
jobs.jsonl        coordinator.db    blobs/
unit-inputs/      results/
```

`run_meta.json` records an ISO UTC `started_at`, arm label, repetition, seed, duration,
lowercase SHA-256 config digest, and git commit. `started_at` marks the workload and churn
origin immediately after the baseline, so baseline power samples occupy negative relative
seconds and legacy churn offsets rebase correctly. Analysis and orchestration share the
same `RUN_FILES` definition so their filenames cannot drift.

### 3.5 Analyze paired runs

Analysis currently compares run directories but does not aggregate repetitions. Produce
one cross-arm report per paired seed:

```bash
python -m fallow_bench analyze \
  --runs dedicated=experiments/runs/dedicated/rep-01 \
         round_robin=experiments/runs/round_robin/rep-01 \
         churn_v2=experiments/runs/churn_v2/rep-01 \
  --out experiments/reports/rep-01 \
  --baseline-start -30 --baseline-end 0
```

Repeat for repetitions 2 and 3, or use nine unique labels for a diagnostic report. Do
not treat a single report as a statistical reduction across repetitions.

The smoke acceptance uses the real in-process coordinator, a loopback SSE replica, and
fake batch workers. It requires every canonical file and zero loader warnings without a
model server or GPU. Its explicit empty `power.jsonl` marks energy as unavailable; every
other applicable headline metric is populated.

### 3.6 Seeds and determinism

Scheduling and analysis logic use injected clocks and seeded randomness. Production
adapters read real UTC timestamps and hardware power samples during a live run; tests
replace those adapters. No experiment logic uses unseeded randomness, so `analyze`
remains reproducible on the same inputs. Specifically:

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
| **units/hour** | distinct `work_unit_id`s in state `done` ÷ elapsed hours of the log | `units.jsonl` |
| **recovery time** | time from a successful `agent_kill` to completion of its leased unit on another agent | `churn.jsonl` + `units.jsonl` |
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
