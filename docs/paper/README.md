# Fallow scheduling study

This directory holds the paper structure for the scheduling experiment. The live
study has not run, so this draft makes no measurement claims. The protocol and
analysis code are complete; the results files remain placeholders until an operator
runs the nine canonical experiments on the study fleet.

## Abstract

Fallow schedules private inference and batch work on desktop machines while they are
idle. This study compares a dedicated machine with two policies on a shared fleet:
round robin and a churn-aware scheduler. The experiment uses three paired seeds. Each
seed fixes the workload across all arms and the churn schedule across the two distributed
arms. The study measures interactive latency, batch throughput, recovery after agent
failure, yield time when a user returns, marginal energy, and the share of requests
served on premises.

Results and conclusions are intentionally absent. Add them only after the live run
directories pass the checks in [Recording results](#recording-results).

## Research question

> Given a fleet of shared desktops that people are actively using, how much useful
> private inference and batch compute can Fallow harvest without the users noticing,
> and does a churn-aware scheduler beat simple baselines on the measured outcomes?

The phrase "without the users noticing" is represented by time-to-yield after a
simulated user return. The study does not survey users or measure subjective notice.

The main policy comparison is `churn_v2` against `round_robin` on the same distributed
fleet. The `dedicated` arm supplies a practical single-machine reference, but it changes
both the scheduler and the fleet. Differences involving that arm cannot be attributed to
scheduler policy alone.

## Method

### Design

The canonical plan contains nine two-hour runs. Each arm has three repetitions, using
seeds 17, 29, and 43. For a given seed, the workload schedule is fixed across all arms,
and the synthetic churn schedule is fixed across the two distributed arms. The runner
executes the arms in this order: `dedicated`, `round_robin`, then `churn_v2`.

| Arm | Coordinator policy | Fleet | Churn |
| --- | --- | --- | --- |
| `dedicated` | `capability` | one dedicated agent | none |
| `round_robin` | `roundrobin` | full distributed fleet | seeded |
| `churn_v2` | `churn_v2` | full distributed fleet | seeded |

The dedicated agent has its own clean coordinator database. The two distributed arms
start from copies of the same clean fleet database. Each run receives an isolated
coordinator database, output directory, API key, and process. Historical idle sessions
used by `churn_v2` come from an immutable input file rather than the run's event log.

### Workload and churn

The workload combines an open-loop interactive request stream with batch embedding
work. Interactive arrivals are scheduled before a run from the repetition seed, while
the batch submission schedule comes from the fixed experiment configuration. A slow arm
does not delay later submissions, so queueing remains part of the observation.

The distributed arms replay independent synthetic idle and active sessions for each
agent. The checked-in configuration schedules simulated user returns. Agent failure and
network loss are optional churn events; enabling either one changes the study method and
must be recorded before the runs begin. The dedicated arm writes an empty churn log.

The bench listener simulates user input on experiment agents. It is unauthenticated and
must bind only to loopback or a trusted tailnet address. It is not part of a normal
deployment.

### Execution

The runner checks the clean seed databases, fleet membership, model readiness, required
inputs, output path, and coordinator process before workload activity begins. It records
a 30-second idle power baseline at 1 Hz, then starts workload and churn at the run origin.
Every live run lasts 7,200 seconds. The complete operator procedure and run directory
contract are in the [experiment protocol](../experiment.md).

Each run records its arm, repetition, seed, duration, configuration digest, source
revision, and UTC start time in `run_meta.json`. The canonical logs and metadata remain
with the run directory so analysis can be repeated from the original inputs.

### Outcomes

B3 renders these rows without changing their labels or order:

| B3 row | Role in the study |
| --- | --- |
| `TTFT p50 (s)` | median time to first token |
| `TTFT p95 (s)` | tail time to first token |
| `Decode tok/s p50` | median decode rate |
| `Batch units/hour` | completed batch units per observed hour |
| `Failure-recovery (s)` | median time from a successful agent kill to completion elsewhere |
| `Time-to-yield p50 (ms)` | median yield time after user return |
| `Time-to-yield p99 (ms)` | tail yield time after user return |
| `Marginal energy per 1k tokens (J)` | energy above the idle baseline per 1,000 tokens |
| `% served on-prem` | share of served, shed, and errored requests served locally |

A missing or inapplicable measurement is rendered with the B3 missing-value marker. It
must not be replaced with zero. Loader warnings are part of the analysis record and must
be resolved or disclosed before drawing conclusions.

## Recording results

Analyze one paired seed at a time. B3 does not combine repetitions, estimate uncertainty,
or run significance tests. The three result directories are therefore separate data
records:

- [repetition 1, seed 17](results/rep-01/report.md)
- [repetition 2, seed 29](results/rep-02/report.md)
- [repetition 3, seed 43](results/rep-03/report.md)

For each repetition, point `--out` at the matching directory. For example:

```bash
python -m fallow_bench analyze \
  --runs dedicated=experiments/runs/dedicated/rep-01 \
         round_robin=experiments/runs/round_robin/rep-01 \
         churn_v2=experiments/runs/churn_v2/rep-01 \
  --out docs/paper/results/rep-01 \
  --baseline-start -30 --baseline-end 0 \
  --git-sha "<revision from the paired run metadata>"
```

Repeat with `rep-02` and `rep-03`. Each invocation replaces the placeholder
`report.md` and writes the exact B3 artifact set beside it:

```text
report.md
report.tex
ttft_cdf.png
yield_cdf.png
throughput_timeline.png
```

Before treating a report as evidence, confirm that its three run directories share the
expected seed, revision, and duration. Check each configuration digest against the
intended template for that arm. Review every analysis warning. Keep the generated table
and plots unchanged; any cross-repetition summary needs a declared method and a
reproducible implementation before it enters the paper.

## Results

No live results are available yet. The generated B3 artifact directories are the source
for all future values, plots, and comparisons in this section.

After the runs, report what happened in each paired repetition before discussing any
pattern across repetitions. Distinguish missing values from observed zeros, and identify
which comparisons involve the same fleet. Do not infer user experience from time-to-yield
alone.

## Threats to validity

### Construct validity

Time-to-yield measures how quickly Fallow suspends work after a simulated return. It does
not measure whether a person noticed fan noise, heat, battery drain, network use, or a
brief performance change. Likewise, `% served on-prem` measures routing outcomes rather
than response quality.

Batch throughput uses the time span between recorded unit events. Sparse activity can
make that window differ from the configured run duration. Failure recovery is defined
only for a killed agent whose leased unit later completes on another agent, so it is not
available when no qualifying failure occurs.

### Internal validity

The paired seeds hold generated schedules constant, but the live system still depends on
clock synchronization, background host activity, model readiness, network conditions,
and power-sampling coverage. The run metadata and isolated databases make these factors
auditable but do not remove them.

The churn-aware policy learns from a fixed historical event file. A mismatch between that
history and the replayed churn can change the policy comparison. Synthetic user returns
also exercise the bench listener rather than the operating system's physical input path.

### External validity

The results describe the tested fleet, model, corpus, arrival rates, and churn parameters.
They do not establish performance for larger models, other hardware, larger fleets, or
different office schedules. The dedicated arm is a one-machine reference, not a controlled
policy ablation against the distributed arms.

### Measurement and analysis limits

Power data may come from a wall-socket meter or a software estimate. Software estimates
can omit power-supply losses and platform components. Reports must identify the source
used by each host and leave energy blank when coverage is inadequate.

Three paired repetitions support a descriptive comparison, not a general population
claim. B3 reports each repetition and does not provide confidence intervals or a
cross-repetition estimator. Any later aggregation must be specified before it is used.

## Reproducibility record

Retain the nine run directories, immutable churn-history input, seed databases, and the
five B3 artifacts for each repetition in an access-controlled study archive. Record the
hardware inventory, model digest, experiment configuration, power source,
clock-synchronization check, and operator notes alongside them. A public artifact bundle
must use sanitized logs and metadata, and it must exclude databases, secrets, and bearer
tokens.

The source protocol is [docs/experiment.md](../experiment.md). The orchestration decision
is [ADR 026](../adr/026-experiment-orchestration.md), and the analysis contract is
[ADR 021](../adr/021-bench-analysis.md).
