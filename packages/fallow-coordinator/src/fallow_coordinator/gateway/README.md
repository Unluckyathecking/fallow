# gateway — OpenAI-compatible inference gateway (module C5)

A FastAPI `APIRouter` that authenticates API keys, resolves a live replica, and
proxies `/v1/chat/completions` and `/v1/embeddings` **verbatim** to llama-server.
Every interactive request produces one audit record; the served-vs-shed ratio in
that log is the experiment's "% served on-prem" metric.

## Public API

Re-exported from `fallow_coordinator.gateway`:

- `create_gateway_router(registry, pick_replica, client, config, request_log, now, quotas=None) -> APIRouter`
  - `registry: GatewayRegistry` — `authenticate_api_key`, `replica_endpoints`,
    `list_models` (module C2's `SqliteRegistry` satisfies it structurally).
  - `pick_replica: PickReplica` — `(model_id, Sequence[ReplicaEndpoint]) ->
    ReplicaEndpoint | None`, injected by the app layer (it passes
    `policy.pick_replica`) so the gateway never imports the scheduler.
  - `client: httpx.AsyncClient` — the upstream transport (injected; tests pass a
    `MockTransport`).
  - `config: GatewayConfig` — connect / first-byte / inter-chunk timeouts.
  - `request_log: RequestLog` — sink for one `GatewayLogEntry` per request.
  - `now: Callable[[], datetime]` — injected clock (deterministic under test).
  - `quotas: QuotaManager | None`: optional per-key RPM and UTC-day enforcement.
- `GatewayConfig`, `GatewayLogEntry`, `LogStatus`, `JsonlRequestLog`,
  `InflightTracker`, and the `GatewayRegistry` / `RequestLog` / `PickReplica`
  seams.

### Routes

- `POST /v1/chat/completions`, `POST /v1/embeddings` — bearer API key. Only the
  `model` field is parsed out of the body (once); everything else is forwarded
  raw — llama-server owns request semantics. `stream: true` yields an SSE
  passthrough.
- `GET /v1/models` — the catalogue in OpenAI list shape, filtered to the key's
  allowlist.

### Error envelope

Every gateway-originated failure uses `{"error": {"message", "type"}}`:
`401` (missing/bad key), `403` (model outside the key allowlist), `404`
(`model_not_found`), `503` (`no_replica_available` — the shed case), `502`
(`upstream_error`, no replica reachable after retry), and `429`
(`rate_limit_error`, with an integer `Retry-After` header).

## Invariants

- **Raw SSE passthrough.** Streaming forwards `httpx.aiter_raw()` bytes
  unchanged — the `[DONE]` line and every byte between are byte-for-byte
  identical. SSE is never parsed or re-serialised.
- **Stream lifetime spans the response.** The upstream stream is *not* wrapped in
  `async with` in the handler (that closes it before the body is sent — the
  premature-close trap). The open response and its `InflightHold` are handed to
  the body generator, which `aclose()`s and releases in a `finally`, so a client
  disconnect still frees the connection and the inflight slot.
- **Before-first-byte retry, once.** A connect error / timeout / 5xx (or a
  first-byte-guard timeout) before any byte reaches the client triggers exactly
  one retry on a *different* endpoint (the failed one is excluded and
  `pick_replica` re-chooses). After the first byte, a mid-stream failure
  terminates the stream cleanly (truncated response) — a POST that reached the
  backend is never replayed.
- **Timeouts.** `connect=2s` and inter-chunk `read=15s` are enforced by the httpx
  transport `Timeout`; the `30s` first-byte budget is enforced separately via
  `asyncio.wait_for` on the first chunk, because the first token can lag far
  behind subsequent ones (prompt eval / model load).
- **Shed = the metric.** A `pick_replica` returning `None` logs `status=shed` and
  returns `503`; this is what makes a request count against "% served on-prem".
- **Log attribution is post-retry.** `agent_id` in the log is the replica that
  actually served, not the first pick.
- **Quota checks precede routing.** Accepted requests consume the key's token bucket and
  daily counter. A rejected request reaches no replica and consumes neither counter.

## Inflight seam

`create_gateway_router` returns a router carrying `get_inflight()` (from the
internal `InflightTracker`): live `(host, port) -> count` for requests currently
proxied, held for the whole duration including a streaming body. The router also
enriches `ReplicaEndpoint.inflight` with this live count *before* calling
`pick_replica`, so routing sees fresher load than the ~5s-old heartbeat value.
The app layer may read `get_inflight()` for its own dashboards/decisions.

## Boundaries

Depends only on `fallow_coordinator.registry` (public API) and
`fallow_protocol`; it never imports the scheduler or modelserve (DAG siblings —
enforced by import-linter). Tests use `httpx.ASGITransport` against the app and
an `httpx.MockTransport` playing llama-server — no real network, no llama-server,
no GPU.
