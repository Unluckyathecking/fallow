# Module A5 — coordinator client + heartbeat loop

The agent's uplink to the coordinator. Every agent→coordinator interaction —
registration, periodic heartbeats, immediate event push, batch-work
long-polling, and result reporting — flows through this module. All I/O is
injected (`httpx.AsyncClient`, clocks, sleeps, hardware probes), so the whole
module is deterministic and network-free under test.

It sits in the agent's middle layer (`heartbeat | workers`): it composes
`idle`, `preempt`, and `supervisor` through their `fallow_protocol.interfaces`
ABCs and never imports the sibling `workers` package.

## Public API

Re-exported from `fallow_agent.heartbeat`:

- `CoordinatorClient(*, base_url, client, agent_id=, device_token=, retry=, sleep=)`
  — typed, retrying HTTP client. Methods: `register`, `heartbeat`, `push_event`,
  `poll_work`, `complete_unit`.
- `HttpEventSink(*, client, jsonl_path, config=, sleep=)` — non-blocking,
  durable `EventSink`.
- `HeartbeatLoop(*, client, agent_id, protocol_version, interval_s, preemptor,
  supervisor, idle, lease_ids, metrics, on_response, on_auth_error, now=, sleep=)`
  — the periodic beacon.
- `collect_caps(agent_version, *, system=, gpu=) -> DeviceCaps` and
  `collect_heartbeat_metrics(*, system=, gpu=) -> HeartbeatMetrics` — host
  telemetry behind the `SystemProbe` / `GpuProbe` seams.
- `SystemProbe` / `GpuProbe` seams and their `PsutilSystemProbe` / `NvmlGpuProbe`
  defaults.
- `ClientRetryConfig` / `EventSinkConfig` — frozen tuning.
- `CoordinatorError` → `CoordinatorAuthError` / `CoordinatorTransientError` /
  `CoordinatorProtocolError`.

## Endpoints (coordinator v1)

| Call            | Method + path                                   | Auth   |
|-----------------|-------------------------------------------------|--------|
| `register`      | `POST /v1/agents/register`                       | none (enrollment token in body) |
| `heartbeat`     | `POST /v1/agents/{id}/heartbeat`                 | bearer |
| `push_event`    | `POST /v1/agents/{id}/events`                    | bearer |
| `poll_work`     | `GET  /v1/agents/{id}/work?timeout=N` (204 → None) | bearer |
| `complete_unit` | `POST /v1/agents/{id}/work_units/{unit}/result`  | bearer |

## Error taxonomy and retries

- `CoordinatorAuthError` — 401/403. Never retried; the heartbeat loop stops and
  fires `on_auth_error`.
- `CoordinatorTransientError` — transport failure (connect/read/reset) or a 5xx
  response. Idempotent calls (`heartbeat`, `poll_work`) retry *transport* errors
  in-line with injected sleep + exponential backoff (`ClientRetryConfig`). 5xx
  is classified transient but not retried in-line — the caller decides.
- `CoordinatorProtocolError` — contract violation (unexpected status, malformed
  body, missing device token). Deterministic; never retried.

`register` is **never** retried (a duplicate enrollment is not idempotent).

## Invariants

- `emit()` is hot-path safe: it only enqueues onto an unbounded thread-safe
  queue and schedules a wakeup — it never blocks the poll thread and never does
  network I/O. The local `events.jsonl` is the durable copy; a coordinator push
  is best-effort and is dropped after `max_push_attempts`.
- The heartbeat loop is un-killable short of an auth rejection: transient,
  protocol, and unexpected errors are logged and the loop keeps beating. `seq`
  increases by one every iteration (including failed ones).
- The client is a stateful *connection* object — it holds `agent_id` and the
  bearer `device_token` learned at registration. The wire messages themselves
  stay frozen.
- `NvmlGpuProbe` never crashes a machine without an NVIDIA GPU: NVML is imported
  lazily and every failure degrades to an empty GPU tuple.

## Testing seams

`httpx.AsyncClient` (driven by `httpx.MockTransport`), `sleep`, `now`, `metrics`,
`lease_ids`, and the `SystemProbe`/`GpuProbe` probes are all injected. Tests use
no real network, no llama-server, and no GPU.
