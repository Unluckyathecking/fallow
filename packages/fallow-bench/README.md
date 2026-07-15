# fallow-bench

`fallow-bench` provides the seeded workload driver, fleet-churn injector, canonical
experiment runner, and deterministic analysis pipeline used by Fallow's scheduling study.

Run the full nine-run plan:

```bash
python -m fallow_bench experiment \
  --config experiments/main.yaml \
  --dedicated-seed-db experiments/seed-dedicated.db \
  --seed-db experiments/seed-fleet.db \
  --churn-history experiments/churn-history.jsonl \
  --host 100.x.y.z \
  --revision "$(git rev-parse HEAD)" \
  --out experiments/runs
```

Use `--smoke`, `--arm`, or `--repetition` to narrow the plan. Analyze one paired
repetition with explicit labels:

```bash
python -m fallow_bench analyze \
  --runs dedicated=experiments/runs/dedicated/rep-01 \
         round_robin=experiments/runs/round_robin/rep-01 \
         churn_v2=experiments/runs/churn_v2/rep-01 \
  --out experiments/reports/rep-01
```

See the [experiment protocol](../../docs/experiment.md),
[orchestration module](src/fallow_bench/experiment/README.md),
[ADR 026](../../docs/adr/026-experiment-orchestration.md),
[spike results](../../experiments/spikes/RESULTS.md), and [license](../../LICENSE).
