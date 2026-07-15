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
* `quotas: QuotaManager | None` optionally enforces per-key RPM and UTC-day limits.

The package also exports `AffinityMap`, `AffinityState`, `GatewayConfig`,
`GatewayLogEntry`, `InflightTracker`, `JsonlRequestLog`, and `LogStatus`.

## Request handling

The gateway reads the request body once. It extracts the model, streaming flag,
prompt character count, and first string-valued user message. llama-server owns
all other request semantics. The gateway forwards the original body bytes.

Every gateway error uses `{"error": {"message", "type"}}`. Authentication
failures return 401, a disallowed model returns 403, an unknown model returns
404, no healthy replica returns 503, and exhausted upstream attempts return 502.
A request over its per-key quota returns 429 with an integer `Retry-After` header.

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

## Session affinity

Clients can send `X-Fallow-Session` to keep a model session on one healthy
replica. The gateway hashes this header together with the bearer API key before
storing it, so two API keys cannot share a sticky mapping by choosing the same
header value. Without the header, a chat request hashes the bearer API key with
the first 256 characters of its first user message. A request without either
signal follows the scheduler without creating a mapping.

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

## Quotas

When a `QuotaManager` is configured, the gateway consumes the key's token bucket
and UTC-day counter before routing. A request over either limit returns `429`
with an integer `Retry-After` header, reaches no replica, and consumes neither
counter. See [ADR 030](../../../../../docs/adr/030-api-key-quotas.md) for the
full decision.

## Admission

When no replica is ready, the gateway waits in a FIFO lane for that model. It
waits for up to `admission_timeout_s`, which defaults to 10 seconds. The shared
waiting room accepts at most `admission_capacity` requests across all model
lanes, with a default capacity of 64. Overflow and timeout return `503`. Batch
work stays on its durable queue and does not enter this waiting room.

Each admission probe runs session affinity first, then asks the scheduler on a
miss. This lets a waiting session return to its healthy mapped replica. Every
gateway record includes `waited_ms` and `affinity`. A request cancelled while it
waits is logged as `cancelled`, not `shed`, and still records its elapsed wait.

See [ADR 029](../../../../../docs/adr/029-interactive-admission.md) for the full
decision.

## Streaming and retries

Streaming uses `httpx.Response.aiter_raw()`. The gateway does not parse SSE, so
the `[DONE]` line and all other response bytes pass through unchanged.

The proxy may retry once on a different endpoint before any byte reaches the
client. It retries connection failures, timeouts, upstream 5xx responses, and a
first-byte timeout. After the first byte, an upstream failure truncates the
response without replaying the POST.

The log's `agent_id` identifies the endpoint that served after any retry, not
the first endpoint attempted. Served and error records keep both `affinity` and
`waited_ms`.

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

See [ADR 031](../../../../../docs/adr/031-slot-aware-inflight-routing.md) for the
reported and local count merge rule.
