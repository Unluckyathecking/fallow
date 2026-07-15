# ADR 031: Slot-aware inflight routing

Status: accepted

Date: 2026-07-15

## Context

The gateway counts requests that pass through its own process. That count is
current, but it misses work sent directly to llama-server and work observed by a
different coordinator process. llama-server already exposes per-slot processing
state, and `ReplicaStatus.inflight` already crosses the agent heartbeat boundary.

The deployed llama.cpp build is pinned to b4589. In that build, `/slots` is
disabled unless llama-server starts with `--slots`. Its response is an array of
slot objects with a boolean `is_processing` field. Upstream labels the endpoint
as a debugging interface, so the agent must expect it to disappear or change.

## Decision

The llama-server command includes `--slots` after manifest arguments. This makes
the monitoring endpoint available even if an older manifest contains a
conflicting option.

Each child process keeps its existing health thread. After the child reaches
READY, that thread polls `/slots` at the normal health cadence and counts entries
where `is_processing` is true. It does not start another thread. The supervisor
stores the last valid count and publishes it through the existing
`ReplicaStatus.inflight` field.

The parser accepts only the b4589 shape. A missing endpoint, non-200 response,
timeout, malformed body, or unexpected exception leaves the previous count
unchanged. A replica starts at zero until its first valid response. The
supervisor writes one debug message per child when occupancy is unavailable and
continues checking whether the process has exited. Suspended replicas skip the
slot request until they resume.

The heartbeat message and protocol version do not change. The coordinator
registry already stores `ReplicaStatus`, so no new wire field or database column
is needed.

Before the scheduler picks a replica, the gateway computes the larger of the
agent-reported count and its process-local counter for each endpoint. The local
counter may be newer, while the reported count can include work the gateway did
not see. Taking the maximum avoids double counting a request that appears in
both sources.

## Consequences

Routing prefers a replica with fewer observed busy slots even when the gateway
has no request history. The scheduler's existing host and port tie-break still
applies when counts match.

Occupancy can lag by one heartbeat interval plus one supervisor poll interval.
The gateway's local counter covers requests that start during that window.

The `/slots` contract is not stable upstream. A future llama.cpp upgrade must
check the launch flag and parser against that pinned release before changing the
parser. Until then, an incompatible response safely degrades to the last count
or zero.

The probe runs only on the agent's configured bind address. It does not add a
new coordinator-to-agent request or change the trusted tailnet boundary.

Pinned source: [llama.cpp b4589 server documentation](https://github.com/ggml-org/llama.cpp/blob/b4589/examples/server/README.md#get-slots-returns-the-current-slots-processing-state).
