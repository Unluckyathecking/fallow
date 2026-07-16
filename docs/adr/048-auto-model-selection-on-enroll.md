# ADR 048: Automatic device-compatible model selection on enroll

**Status:** accepted

**Date:** 2026-07-16

## Context

A machine joins the fabric by enrolling, but it serves nothing until an operator
runs `flw assign`. That hand step does not scale: a real fleet of idle
desktops should each pick the best model they can actually run and start serving
on their own. ADR 039 already gave us the primitive we need — `model_fit`
compares a model's declared minimums (`min_ram_mb`, `min_vram_mb`) against an
agent's reported capacity and answers "can this agent hold this model". What was
missing is the coordinator picking a model with it and assigning it at enroll.

The join itself is already automatic: once an assignment exists, the agent
downloads the blob and launches its worker on the next heartbeat. So the only new
behavior is *placement* — a coordinator-internal choice over the existing caps
and model registry. No wire type changes.

## Decision

### Opt-in, off by default

A new `CoordinatorConfig.auto_assign_on_enroll: bool = False`. Existing
deployments keep the operator-driven flow unchanged; a fleet that wants
self-service turns it on. Nothing about `flw assign` changes either way.

### Fit against declared caps, not the live heartbeat

At enroll the agent has sent no heartbeat, so its *live* free RAM and VRAM read
as zero — every model would "not fit". Enroll-time fit is instead against what
the machine *is*: total RAM and each GPU's total VRAM from the registration caps.
`capacity_snapshot` projects those caps into an `AgentSnapshot` at full capacity
and feeds it through the same `model_fit`, so enroll and `flw assign` share one
definition of fit rather than growing a second copy.

### Largest that fits, with a stable tie-break

From the models that fit, pick the single largest by `size_bytes`. A GPU-capable
agent prefers a model that actually uses the GPU (`min_vram_mb > 0`) over a
CPU-only one, so idle VRAM does real work rather than sitting behind a model that
would have run on CPU anyway. Ties break on `model_id`, so the choice is
deterministic and stable across restarts and does not depend on registry
ordering. The selector (`select_model_for_agent`) is a pure function over a
snapshot and a model list, which keeps it trivially unit-testable.

### Never override an operator

Auto-assign runs only when the agent has no assignment yet. An agent that already
has one — from an operator or an earlier auto-assign — is left untouched. If
nothing in the registry fits the machine, the enroll still succeeds; the reason
is logged, never raised, so a fleet with no compatible model keeps enrolling
agents that simply stay idle until one is registered.

### Placement lives at the register seam

The selection runs in the register route after the registry commits the agent,
not inside the registry itself. The registry stays a plain store; the app layer
owns the policy of what to place where, consistent with how the admin assignment
route already drives `model_fit`.

## Consequences

- Enroll-time fit uses declared totals, which is deliberately optimistic: it
  ignores whatever else the machine is running. The agent's own preemption and
  the operator's `flw assign` remain the authority on live capacity; auto-assign
  only chooses an initial model that the hardware can hold in principle.
- With the flag on, the first agent to enroll after a large model is registered
  will claim it. This is placement, not balancing — spreading load across the
  fleet stays the scheduler's job.
- The selector considers every registered model. Curating what a fleet may
  auto-select is done by controlling what is registered.
