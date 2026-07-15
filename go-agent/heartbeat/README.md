# heartbeat

The Go agent's typed HTTP client to the coordinator. Every agent→coordinator
call goes through `Client`: registration, periodic heartbeats, immediate event
push, long-poll work acquisition, and result completion. It is a 1:1 port of
`fallow_agent.heartbeat.client.CoordinatorClient`.

## What it does

`Client` is a stateful connection object. It holds the `agent_id` and bearer
`device_token` learned at registration — connection state, not domain data. The
wire messages themselves (`protocol` package) stay frozen.

| Method | Verb / path | Success codes | Retried? |
| --- | --- | --- | --- |
| `Register` | `POST /v1/agents/register` | 200, 201 | never (enroll is not idempotent) |
| `Heartbeat` | `POST /v1/agents/{id}/heartbeat` | 200, 201 | transport errors only |
| `PollWork` | `GET /v1/agents/{id}/work?timeout=…` | 200 (lease), 204 (no work) | transport errors only |
| `PushEvent` | `POST /v1/agents/{id}/events` | 200, 201, 202, 204 | no (fire-and-forget) |
| `CompleteUnit` | `POST /v1/agents/{id}/work_units/{unit}/result` | 200, 201, 202, 204 | no (fire-and-forget) |

`Register` sends **no** bearer. All other calls send `Authorization: Bearer
<device_token>`; calling them before registration returns a `*ProtocolError`.

## Status-code semantics

Identical to the Python client:

- 200/201 carry a parseable success body.
- 200/201/202/204 are accepted for fire-and-forget writes.
- 401/403 → `*AuthError` (retrying the same token is pointless).
- ≥500 → `*TransientError` (safe to retry later).
- any other status → `*ProtocolError` (deterministic contract violation).
- a malformed body on an otherwise-OK response → `*ProtocolError`.

## Retry policy

Only idempotent calls (`Heartbeat`, `PollWork`) retry, and only on **transport**
errors (connection failure, reset, timeout) — never on a status code. Backoff is
exponential (`BackoffBase · 2^(attempt-1)`) via an injectable `SleepFunc`.
`RetryConfig.MaxRetries` counts retries *after* the initial attempt, so
`MaxRetries: 2` permits at most three total attempts. 5xx responses map to
`*TransientError` but are **not** retried in-line: the caller (heartbeat loop /
event sink) decides how to react.

## Testing seam

All I/O goes through the `Doer` interface (satisfied by `*http.Client`), so
tests drive the client with an `httptest.Server` or a failure-injecting
`http.RoundTripper` and never guess at sockets. See `client_test.go` for the
behavioral cases ported from the Python suite.
