# HTTP relay v1

Site Mode uses ordinary authenticated HTTPS. It does not introduce a WebSocket or a custom multiplexing format.

## Claim

An available agent holds this request open:

```http
GET /v1/agents/{agent_id}/inference/claims?timeout_s=25
Authorization: Bearer DEVICE_TOKEN
```

The coordinator returns `204 No Content` when the bounded wait expires. A successful claim returns `200 application/json`:

```json
{
  "version": 1,
  "claim_id": "opaque-random-id",
  "presence_generation": 42,
  "replica_port": 8100,
  "method": "POST",
  "path": "/v1/chat/completions",
  "content_type": "application/json",
  "body_b64": "BASE64_OF_VALIDATED_REQUEST_BYTES",
  "deadline_ms": 30000
}
```

The only v1 paths are `/v1/chat/completions` and `/v1/embeddings`. The method is always `POST`. The decoded request body is bounded at 2 MiB. A claim belongs to the authenticated path agent and one replica port currently reported READY by that agent.

The coordinator grants a claim only while the agent is fresh, idle, not reclaimed, READY for the requested model and waiting for work. One held claim is allowed per available replica slot. Queues and response buffers have fixed limits.

## Response

After opening the local loopback response, the agent streams its raw bytes to:

```http
POST /v1/agents/{agent_id}/inference/claims/{claim_id}/response
Authorization: Bearer DEVICE_TOKEN
X-Fallow-Presence-Generation: 42
X-Fallow-Upstream-Status: 200
Content-Type: text/event-stream
Transfer-Encoding: chunked
```

The coordinator returns:

- `202` after a complete accepted upload;
- `404` for an unknown claim;
- `409` for the wrong owner, duplicate completion or invalid state;
- `410` when the client left, the deadline passed or a newer presence generation invalidated the claim.

The coordinator relays chunks with bounded buffering. It does not parse or reframe SSE. The agent forwards no client Authorization header to llama and may dial only `127.0.0.1` on a port owned by a READY local replica.

## Failure

Before response bytes begin, the agent may report:

```http
POST /v1/agents/{agent_id}/inference/claims/{claim_id}/failure
Authorization: Bearer DEVICE_TOKEN
Content-Type: application/json

{"presence_generation":42,"code":"became_active","retryable":true}
```

Allowed codes are `became_active`, `reclaimed`, `connect_failed`, `timeout`, `cancelled` and `upstream_error`. Free-text remote errors are not part of the wire contract.

A failure before the first response byte may use the gateway's existing single repick. A failure after the first byte truncates the response and is never replayed.

## Presence ordering

The Go agent uses one increasing sequence for heartbeats and presence events. A presence event includes its sequence as decimal text in `AgentEvent.detail["sequence"]`. The coordinator records it as a fence. A delayed heartbeat with a lower sequence cannot make an active agent idle again.

Each claim carries the coordinator's current `presence_generation`. A user-return event, reclaim, stale heartbeat or route removal advances that generation and invalidates queued or claimed work. Response and failure uploads with an older generation are rejected.

Local ordering remains:

1. detect input;
2. suspend replicas;
3. stop or cancel claim work;
4. send the presence event.

The heartbeat is the recovery path if the immediate event fails. It is not the primary user-return signal.

## Resource bounds

The first implementation uses these hard limits unless a focused benchmark justifies a change:

- 2 MiB decoded request body;
- 32 KiB response chunks;
- one held claim per READY replica slot;
- no queued claim when an agent has no waiter;
- 25-second claim wait;
- existing gateway first-byte and inter-chunk deadlines;
- no replay after a claim has produced response bytes.

Coordinator restart loses in-memory claims. Agents reconnect and claim new work; non-idempotent requests are not replayed automatically.
