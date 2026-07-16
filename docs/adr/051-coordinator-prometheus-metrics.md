# ADR 051: Coordinator Prometheus metrics

**Status:** accepted

**Date:** 2026-07-16

## Context

Operators can inspect agents through the admin API and analyze gateway requests
from the JSONL audit log. Neither source can be scraped directly by a standard
Prometheus installation. Fleet dashboards need a small, stable view of agent,
replica, and gateway health without changing how the coordinator schedules work.

## Decision

The coordinator exposes `GET /metrics` in Prometheus text exposition format
0.0.4. The route uses the same admin bearer authentication as `/v1/admin/*`.
Fleet state and request history may reveal model names and usage patterns, so an
open endpoint would disclose operational data that the existing API protects.

The formatter is a pure function over registry snapshots and gateway counters.
It reports online agent counts, ready and stopped replicas per model, gateway
request outcomes, retries, and replica inflight counts. Agent state labels use
the protocol's idle, active, and draining values. Heartbeat suspicion has its
own metric because it can overlap any of those states.

The inflight gauge takes the larger of each replica's heartbeat count and the
gateway's local count. This is the same rule used by routing, and it lets a
scrape see requests that started after the latest heartbeat. The endpoint does
not alter the registry, request log, gateway tracker, or scheduler.

Gateway outcomes already live in the append-only JSONL audit log. The route
reads that file and derives counters for each scrape. Malformed or incomplete
lines are ignored because a scrape should not fail on a partial final write.
This keeps the first version small and avoids another state store or a metrics
dependency.

## Consequences

Counter values survive coordinator restarts because the audit log survives
them. Reading the full file makes scrape cost proportional to request history.
If that cost becomes material, a later change can add a checkpointed counter
without changing metric names or the formatter. The route remains read only,
and no scheduling policy consumes these values.
