# Roadmap

This roadmap communicates direction, not a delivery commitment. Priorities may change as the
project learns from contributors and experiments.

## Shipped in 0.1.0

- Coordinator composed into a runnable, configured service (`fallow_coordinator serve`).
- Agent composed into a supervised daemon with safe shutdown and recovery
  (`fallow_agent run`).
- End-to-end integration/chaos suite; 332 passing tests; live two-machine fleet demo.
- Secure bootstrap (enrollment/device/client tokens), admin credential handling and
  per-machine deployment guidance.
- Preemption target validated on real hardware and published with reproducible numbers
  ([`experiments/spikes/RESULTS.md`](experiments/spikes/RESULTS.md)): p99 end-to-end yield
  103 ms (Mac) / 116 ms (Windows), 1.268 ms real production yield.
- ADRs 000–017, an [architecture overview](docs/architecture.md), and the
  [scheduling-experiment protocol](docs/experiment.md).

## Toward 0.2

- **Run the scheduling experiment.** The three arms are already config-selectable
  (`CoordinatorConfig.scheduler` = `capability` | `roundrobin` | `churn_v2`) and the
  `fallow-bench` workload (B1), churn injector (B2) and analysis (B3) modules exist. What
  remains is wiring the `run` and `analyze` subcommands (the `churn` subcommand is wired),
  writing ADR 022 for the churn-aware scheduler, and executing the three-arm study defined
  in [`docs/experiment.md`](docs/experiment.md).
- **School-pilot hardening.** Linux agents, Defender/SmartScreen allowlisting playbook,
  and unattended install/upgrade paths for a real managed fleet.
- **mTLS or equivalent workload identity** so transport security no longer relies solely on
  the tailnet ([ADR 000 §6](docs/adr/000-architecture-baseline.md)).
- **Result-blob upload** for batch work units (durable, verifiable result storage and
  retrieval).
- **Registry `set_agent_state`** so the gateway interactive path reacts to
  `user_returned`/`user_idle` on the event rather than the next heartbeat — closing the
  batch-vs-gateway asymmetry noted in
  [ADR 014](docs/adr/014-coordinator-app.md) and [ADR 016](docs/adr/016-integration-suite.md)
  open questions.
- Publish an explicit threat model and commission an independent security review.
- Decide package distribution names and establish trusted publishing.

## Later candidates

- Installation packages and service integration for supported operating systems.
- mTLS or equivalent workload identity, key rotation and revocation workflows.
- Rate limiting, multi-tenant policy boundaries and high-availability coordination.
- Observability, upgrade/rollback tooling and compatibility-tested inference backends.

Feature requests are welcome, but work that enables high-risk decision making remains out of
scope. See [`docs/ai-act-scoping.md`](docs/ai-act-scoping.md).
