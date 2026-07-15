# ADR 026: Canonical experiment orchestration

**Status:** Accepted  
**Date:** 2026-07-15

## Context

The scheduling study compares three policies across repeated runs. Manual setup makes it
easy to change a seed, mix files from different arms, omit a required log, or overwrite a
previous result. Those mistakes can invalidate a comparison without causing an obvious
runtime failure.

The workload, churn, and analysis components already have deterministic cores. What was
missing was one command that fixes the study plan and gives every component the same run
identity and file contract.

## Decision

`python -m fallow_bench experiment` owns the canonical plan. It runs three scheduler arms,
three repetitions, paired seeds 17, 29, and 43, and a fixed duration of 7,200 seconds. The
smoke plan uses the same arms and seeds with a duration of 120 seconds.

Each run gets a new `<root>/<arm>/rep-XX` directory. Existing directories are rejected.
Coordinator templates isolate all mutable paths inside that directory. The orchestrator
and analysis package share `RUN_FILES`, which defines the canonical artifact names.
Separate clean coordinator databases define the dedicated one-agent fleet and the full
distributed fleet. The appropriate snapshot is copied into each run before startup. This
preserves device identities while keeping run state isolated. Churn-aware scheduling reads
an immutable historical event log that is separate from the run's output events.
The runtime waits for the exact expected idle-agent set and both required READY models.
Coordinator admin credentials are passed through the child environment and are not written
to the rendered template.

The runner creates `run_meta.json` after fleet readiness, captures a 30-second idle-power
baseline, then finalizes `started_at` as the workload and churn origin. Baseline samples
therefore have negative relative times. The dedicated arm skips churn and records an empty
churn log. Run metadata also includes the arm, repetition, seed, duration, resolved
configuration digest, and git commit.

Runtime boundaries are injected so CI can use an in-process coordinator, loopback SSE
replica, fake batch worker, and controlled clocks. Live runs use the same plan and file
contract with real processes and fleet endpoints.

## Consequences

- Paired arms receive the same seeded workload and churn inputs.
- Every completed run is directly readable by the analysis pipeline.
- Partial output is preserved for diagnosis, while a retry must use a new output root or
  remove the failed directory deliberately.
- Fleet membership and power instrumentation remain operator responsibilities. In
  particular, the dedicated template cannot enforce a one-machine fleet.
- Smoke tests validate orchestration and analysis compatibility, not hardware performance.
- The full live study remains an operator-run activity because it needs the physical fleet.

## Verification

Plan and layout tests pin arm order, paired seeds, durations, collision refusal, metadata,
and phase ordering. Template tests load every rendered coordinator config and verify its
paths remain inside the run directory. The integration smoke uses the real coordinator
wire paths and fake replicas, then runs analysis with no loader warnings.
