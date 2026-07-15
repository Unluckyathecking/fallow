# ADR 028: Gateway session affinity

Status: accepted

Date: 2026-07-15

## Context

Interactive chat often benefits from returning to the same replica. Reusing a
warm prefix cache can reduce prompt work, but a permanent assignment would route
requests to an unhealthy replica and let the coordinator's memory grow without
a bound.

The gateway must also keep its existing transport contract. It parses only the
fields needed for routing and logging, then forwards the original request and
stream bytes unchanged.

## Decision

The gateway keeps an in-memory map from a session key to a replica endpoint. The
map has a sliding idle TTL and an LRU size limit. Both limits come from
`affinity_ttl_s` and `affinity_max`. The map reads the same injected clock as the
rest of the gateway, which makes expiry deterministic in tests.

`X-Fallow-Session` is the preferred client signal. The gateway hashes its value
before using it as a map key. If the header is absent, a chat request derives an
opaque key by hashing the bearer API key with the first 256 characters of the
first string-valued user message. Embedding requests and chat requests without
either signal do not use affinity. The model ID is part of every internal key,
so one client session can use different models without displacing either
mapping.

A mapping is reusable only when its exact agent, host, port, and model still
appear in the registry's current routing candidates. That candidate list already
excludes stale agents, active agents, and replicas that are not READY. A missing
endpoint turns the lookup into a miss, removes the old entry, and returns routing
to the configured scheduler.

The scheduler handles every miss. The chosen endpoint is cached immediately so
concurrent requests for a new session converge. If the proxy retries before the
first byte, the mapping moves to the endpoint that actually served. If every
attempt fails, the mapping is removed.

`GatewayLogEntry.affinity` records `hit`, `miss`, or `none`. It describes the
lookup state at the start of routing. A request can therefore record `hit` and
`retried=true` when its cached endpoint fails and the retry succeeds elsewhere.

## Consequences

Affinity state is local to one coordinator process and disappears on restart.
That is acceptable because the next request returns to normal scheduling and
creates a fresh mapping. Multiple active coordinator processes do not share
affinity state.

The map stores opaque digests instead of bearer tokens, session headers, or
prompt text. Its memory use is bounded by `affinity_max`, and inactive entries
expire after `affinity_ttl_s`.

Health takes priority over cache locality. A suspended, preempted, stale, or
removed replica cannot receive a request merely because it has a cached session.

The gateway still sends the original request body to llama-server. Streaming
continues to use the raw byte iterator, so affinity does not parse or rewrite SSE
frames.
