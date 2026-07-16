# ADR 039: Capability-aware model assignment

Status: accepted

Date: 2026-07-16

## Context

`PUT /v1/admin/assignments` accepts any model-to-agent mapping. Nothing checks
that the target agent can hold the model, so an admin can assign a model that
does not fit an agent's RAM or VRAM. The mistake stays silent until the replica
fails to launch on the agent, far from the request that caused it.

The information to catch this is already on hand. A registered `ModelManifest`
declares `min_ram_mb` and `min_vram_mb`. Every `AgentSnapshot` carries the
agent's latest reported capacity: `mem_available_mb` from the heartbeat and free
VRAM per GPU. No new heartbeat field or database column is needed.

## Decision

A pure fit check compares a manifest's declared minimums against an agent's
latest snapshot: `min_ram_mb` against `mem_available_mb`, and `min_vram_mb`
against the free VRAM of the roomiest single GPU. A replica loads onto one GPU,
so the VRAM check is against the largest device, not the sum. An agent with no
GPU reports zero available VRAM, so any positive VRAM requirement fails there.

On assignment, each target agent that has a live snapshot is checked before any
write. If one does not fit, the whole request is rejected with 409 and a message
naming the model, the agent, and required versus available on both axes.

Reject, not warn. An assignment that cannot launch is a configuration error, and
a 409 surfaces it at the moment it is made. A warning would still let the bad
mapping land and reproduce the original silent failure downstream. The endpoint
keeps its all-or-nothing contract: the check runs entirely before any write, so
a rejected request changes nothing.

Three cases are left to the existing path rather than rejected. An unregistered
model has no minimums to check against. An agent with no current snapshot
(offline, or never seen) has no reported capacity to check against; the check
only constrains agents the coordinator can actually see. An agent already
assigned the model is skipped: it has already paid the model's memory footprint,
so its reported free memory would wrongly fail a re-asserted mapping.

A companion probe, `GET /v1/admin/agents/{agent_id}/fit?model_id=...`, returns
`{fits, required_vram_mb, required_ram_mb, available_vram_mb, available_ram_mb}`
so an operator can check before assigning. It is admin-authenticated and returns
404 for an unknown model or an agent with no live snapshot.

This PR only validates and reports. It does not add automatic placement or
"largest quant that fits" selection; that is later work.

## Consequences

An assignment that would fail to launch on a visible agent is now caught at the
request, with a message that says which agent, which model, and by how much.

The check reads the live snapshot, so its verdict tracks the last heartbeat. An
agent whose free memory dropped since its last heartbeat can still be assigned a
model that no longer fits; the launch-time guard on the agent remains the final
backstop. Offline and unregistered targets are unaffected, matching prior
behaviour.

The fit response is coordinator-local, not a protocol wire type, so no schema
changes. The check lives with the other pure eligibility predicates in the
scheduler and stays clock-free and I/O-free.
