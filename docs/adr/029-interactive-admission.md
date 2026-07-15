# ADR 029: Interactive admission queue

**Status:** accepted  
**Date:** 2026-07-15

## Context

The gateway returned 503 as soon as `pick_replica` found no healthy endpoint. Short
preemption windows therefore counted as shed traffic even when an agent became idle a few
seconds later. Waiting without a bound would move the overload into the coordinator and
could exhaust memory.

Batch work already has a durable queue and does not pass through the gateway. This decision
only covers interactive gateway requests.

## Decision

The gateway has one in-memory waiting room with a default capacity of 64 requests. Requests
wait in FIFO order within each model lane. Only the head request in a lane checks the
registry, so a newer request cannot pass an older request for the same model.

When the first replica lookup fails, the request waits for at most
`admission_timeout_s`, which defaults to 10 seconds. The queue checks for a replica every
50 milliseconds. The monotonic clock and sleep function are injected. A recovered request
continues through the existing proxy and retry path without changing its body.

If the waiting room is full, the gateway returns 503 immediately. If the deadline expires,
it also returns 503. `GatewayLogEntry.waited_ms` records time spent waiting for both served
and shed requests. Requests that never enter the queue record zero.

`CoordinatorConfig.admission_timeout_s` and `admission_capacity` set the two operator
limits. A zero timeout preserves immediate shedding and is useful for deployments that do
not want interactive queueing.

## Consequences

- Short ACTIVE-to-IDLE windows can complete without a client retry.
- The capacity bound limits retained request bodies and tasks.
- Queue state is process-local and disappears on coordinator restart.
- FIFO applies per model. Separate model lanes can make progress independently.
- Admission does not reserve a replica. Normal inflight accounting and scheduler selection
  still run when the request leaves the waiting room.
