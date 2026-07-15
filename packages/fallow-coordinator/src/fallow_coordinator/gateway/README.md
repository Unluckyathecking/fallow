# OpenAI-compatible inference gateway

This package provides the coordinator's `/v1/chat/completions`, `/v1/embeddings`,
and `/v1/models` routes. It authenticates the client, resolves a live replica,
and proxies the request to llama-server. Each interactive request writes one
`GatewayLogEntry` for experiment analysis.

## Public API

`create_gateway_router(registry, pick_replica, client, config, request_log, now)`
accepts narrow collaborators that tests can replace without a network:

* `GatewayRegistry` authenticates keys and supplies models and live endpoints.
* `PickReplica` is the scheduler's replica selection function.
* `httpx.AsyncClient` carries upstream requests.
* `GatewayConfig` contains timeout and affinity limits.
* `RequestLog` receives the terminal record for each request.
* `now` supplies timestamps and affinity expiry time.

The package also exports `AffinityMap`, `AffinityState`, `GatewayConfig`,
`GatewayLogEntry`, `InflightTracker`, `JsonlRequestLog`, and `LogStatus`.

## Request handling

The gateway reads the request body once. It extracts the model, streaming flag,
prompt character count, and first string-valued user message. llama-server owns
all other request semantics. The gateway forwards the original body bytes.

Every gateway error uses `{"error": {"message", "type"}}`. Authentication
failures return 401, a disallowed model returns 403, an unknown model returns
404, no healthy replica returns 503, and exhausted upstream attempts return 502.

## Session affinity

Clients can send `X-Fallow-Session` to keep a model session on one healthy
replica. The gateway hashes this header before storing it. Without the header, a
chat request hashes the bearer API key with the first 256 characters of its first
user message. A request without either signal follows the scheduler without
creating a mapping.

Mappings use a sliding idle TTL and an LRU size limit. Configure them with
`affinity_ttl_s` and `affinity_max`. Model ID is part of the internal key.

A hit is valid only if the mapped agent, host, port, and model remain in the
registry's current candidate set. Stale agents, active agents, suspended or
preempted replicas, and removed endpoints therefore become misses. The scheduler
chooses a replacement, and a successful before-first-byte retry updates the map
to the endpoint that served the response. A failed retry sequence removes the
mapping.

The request log records `affinity` as `hit`, `miss`, or `none`. This value
describes the lookup at the start of routing. Affinity state is process-local and
starts empty after a coordinator restart.

See [ADR 028](../../../../../docs/adr/028-gateway-session-affinity.md) for the
full decision.

## Streaming and retries

Streaming uses `httpx.Response.aiter_raw()`. The gateway does not parse SSE, so
the `[DONE]` line and all other response bytes pass through unchanged.

The proxy may retry once on a different endpoint before any byte reaches the
client. It retries connection failures, timeouts, upstream 5xx responses, and a
first-byte timeout. After the first byte, an upstream failure truncates the
response without replaying the POST.

The open upstream response and its inflight hold live until the response body
finishes or the client disconnects. Cleanup closes the upstream response and
releases the inflight count in either case.

The default connect timeout is 2 seconds. The first byte has a 30 second budget,
and later chunks have a 15 second budget. The transport read timeout sits above
both application-level guards.

## Boundaries

The gateway depends on the registry public API and `fallow_protocol`. It does
not import scheduler or model-serving internals. Import-linter enforces this
boundary. Unit tests use an ASGI transport and an `httpx.MockTransport`; the
integration suite uses loopback llama-server stubs and preserves raw stream
bytes.
