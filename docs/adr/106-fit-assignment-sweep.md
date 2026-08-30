# ADR 106: Fit-assignment sweep

Status: accepted · Date: 2026-08-29 · Module: fallow-coordinator/app, fallow-cli

## Context

Placing a model on the fleet takes one `flw assign` per explicit agent list.
That is fine for a handful of known desks and wrong for the actual deployment
shape: machines of unknown capacity enrolling one at a time while a batch job
is already queued. The operator wants "every node that can hold this job's
model should pull it" without reading `flw agents list` and typing ids, and
without the coordinator ever moving models around on its own.

The pieces already exist. `PUT /assignments` fit-checks targets before
writing (409 on any unfit agent), heartbeats deliver `desired_models`, and the
agent's reconcile loop downloads and launches whatever appears there. Enroll-
time auto-assign (ADR 048) covers machines that join after a model is
registered but does nothing for machines that were already enrolled.

## Decision

1. **One endpoint, one sweep.** `POST /v1/admin/assignments/fit
   {"model_id"}` walks the live agent snapshots once and assigns the model to
   every agent that has no assignment and passes the existing `model_fit`
   gate. It runs when the operator calls it and never again: no background
   loop, no standing policy. The CLI exposes it as `flw assign <model-id>
   --fit`, and `flw jobs submit --assign-fit` runs it right after queueing a
   job so the job's model reaches the fleet in the same command.
2. **Skip, don't reject.** `PUT /assignments` stays all-or-nothing because the
   operator named specific agents. A sweep names nobody, so a machine that
   cannot hold the model is reported (`skipped`, with the fit numbers), not a
   reason to fail the request. Agents already serving the model come back as
   `kept`; agents with no recent heartbeat come back as `offline` and are left
   untouched for a later sweep.
3. **An assigned agent is never reassigned.** The sweep only touches agents
   with an empty assignment, mirroring ADR 048's rule that operator intent is
   never overridden. This also makes tiering deterministic: sweep the largest
   model first and it claims the machines that fit it, then sweep the smaller
   one for the rest. Moving an agent off a model it already holds stays an
   explicit `flw assign` with names.

## Consequences

- Bringing a fleet of unknown machines onto a job is now `flw jobs submit
  --assign-fit` plus re-running `flw assign <model> --fit` (or relying on
  enroll-time auto-assign) as stragglers come online.
- The fit gate reads the agent's latest heartbeat, so a machine busy with its
  user may report too little free memory and be skipped; the next sweep picks
  it up. A registrant that has not heartbeated yet reports zero free capacity
  and is skipped the same way rather than assigned blind.
- The response is the audit record: who gained the model, who already had it,
  who could not take it and why, who was unreachable. Nothing else logs the
  sweep.
- Two sweeps racing each other can both see an agent as unassigned; the last
  per-agent write wins, which is the same behaviour two concurrent `flw
  assign` calls already have. Sweeps are operator actions, not loops, so this
  stays acceptable.
