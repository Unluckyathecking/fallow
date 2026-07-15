# OpenAI-compatible inference gateway

This package provides `/v1/chat/completions`, `/v1/embeddings`, and `/v1/models`.
It authenticates the client, resolves a live replica, and proxies the original
request bytes to llama-server. Each interactive request writes one
`GatewayLogEntry` for experiment analysis.

## Public API

`create_gateway_router(registry, pick_replica, client, config, request_log, now)`
accepts small collaborators that tests can replace without a network:

* `GatewayRegistry` authenticates keys and supplies models and live endpoints.
* `PickReplica` is the scheduler's replica selection function.
* `httpx.AsyncClient` sends upstream requests.
* `GatewayConfig` contains connect, first-byte, and inter-chunk timeouts.
* `RequestLog` receives the terminal record for each request.
* `now` supplies request timestamps and liveness query time.

## Request handling

The gateway reads the request body once and extracts only the model, streaming
flag, and prompt character count. llama-server owns all other request semantics.
The gateway forwards the original request body.

Every gateway error uses `{"error": {"message", "type"}}`. Authentication
failures return 401, a disallowed model returns 403, an unknown model returns
404, no healthy replica returns 503, and exhausted upstream attempts return 502.

## Inflight routing

Agents report llama-server's busy slot count in the existing
`ReplicaStatus.inflight` field. The gateway also counts requests currently
passing through its own process. Before calling the scheduler, it sets each
endpoint's inflight value to the larger of these two counts.

The maximum matters because the sources overlap. Adding them would count a
gateway request twice after the next heartbeat. Taking only the local count
would miss direct llama-server work and requests seen by another coordinator.

The capability and churn-aware schedulers prefer the least busy replica before
their host and port tie-break. Round-robin keeps its experiment behavior and
does not become load-aware.

## Streaming and retries

Streaming uses `httpx.Response.aiter_raw()`. The gateway does not parse SSE, so
the `[DONE]` line and all other response bytes pass through unchanged.

The proxy may retry once on a different endpoint before any byte reaches the
client. It retries connection failures, timeouts, upstream 5xx responses, and a
first-byte timeout. After the first byte, an upstream failure truncates the
response without replaying the POST.

The open upstream response and its inflight hold live until the body finishes or
the client disconnects. Cleanup closes the upstream response and releases the
local count in either case.

## Boundaries

The gateway depends on the registry public API and `fallow_protocol`. It does
not import scheduler or model-serving internals. Import-linter enforces this
boundary. Unit tests use ASGI and mock transports. Integration tests use
loopback llama-server stubs.

See [ADR 031](../../../../../docs/adr/031-slot-aware-inflight-routing.md) for the
reported and local count merge rule.
