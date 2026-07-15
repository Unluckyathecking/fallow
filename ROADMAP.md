# Roadmap

This roadmap communicates direction, not a delivery commitment. Priorities may change as the
project learns from contributors and experiments.

## Toward 0.1.0

- Compose the coordinator modules into a runnable, configured service.
- Compose the agent modules into a supervised daemon with safe shutdown and recovery.
- Complete the benchmark workload, churn and analysis harness.
- Add end-to-end tests across macOS, Linux and Windows agents.
- Define secure bootstrap, admin credential handling and deployment guidance.
- Publish an explicit threat model and commission an independent security review.
- Validate the preemption target on representative hardware and publish reproducible results.
- Decide package distribution names and establish trusted publishing.

## Later candidates

- Installation packages and service integration for supported operating systems.
- mTLS or equivalent workload identity, key rotation and revocation workflows.
- Rate limiting, multi-tenant policy boundaries and high-availability coordination.
- Observability, upgrade/rollback tooling and compatibility-tested inference backends.

Feature requests are welcome, but work that enables high-risk decision making remains out of
scope. See [`docs/ai-act-scoping.md`](docs/ai-act-scoping.md).
