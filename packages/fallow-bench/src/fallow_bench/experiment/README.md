# Experiment orchestration

This package turns the scheduling protocol into one reproducible run plan.

`plan.py` defines the three arms, paired seeds, repetitions, and live or smoke durations.
`layout.py` allocates a new directory for each run using the filenames exported by
`fallow_bench.analysis.RUN_FILES`. `templates.py` renders the arm's isolated coordinator
configuration. `runner.py` owns metadata, baseline ordering, concurrent workload and
churn, cancellation, and cleanup. `cli.py` provides the `experiment` command and optional
arm or repetition filters.

The canonical order is all three `dedicated` repetitions, followed by `round_robin`, then
`churn_v2`. The dedicated arm does not start churn and still writes an empty
`churn.jsonl`. Output directories are never reused automatically.

Runtime work stays behind injected phase seams. Production adapters own processes,
network clients, credentials, and clocks. Tests use an in-process coordinator and fake
replicas, but produce the same files as a live run.

The live runner always starts the coordinator as a subprocess. Package code must not
import coordinator internals; in-process shortcuts belong only in integration tests.

Live runs use separate clean, checkpointed seed databases for the one-agent dedicated arm
and the full distributed fleet. The runtime copies the appropriate snapshot into each run
before starting its isolated coordinator. Churn-aware runs also require an immutable
historical event log that is kept separate from the run's output events.

The directory contract is:

```text
<root>/<arm>/rep-XX/
  coordinator.toml
  run_meta.json
  client_trace.jsonl
  gateway.jsonl
  events.jsonl
  churn.jsonl
  power.jsonl
  units.jsonl
  schedule.jsonl
  jobs.jsonl
```

The same directory also owns the coordinator database, blobs, unit inputs, and result
payloads. See [`docs/experiment.md`](../../../../../docs/experiment.md) for operator steps
and [ADR 026](../../../../../docs/adr/026-experiment-orchestration.md) for the decision.
