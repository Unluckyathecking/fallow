# ADR 012: OpenAI-compatible gateway (module C5)

Status: accepted · Date: 2026-07-15

## Context

Clients speak the OpenAI wire protocol. The coordinator must authenticate them,
route each request to an idle replica, and forward the request to llama-server
without owning its semantics — while measuring how much interactive traffic is
served on-prem versus shed.

## Decisions

1. **Parse only `model`; forward the body verbatim.** `json.loads` runs once to
   read `model` (routing), `stream` (transport), and `prompt_chars` (audit).
   Everything else is llama-server's contract; the raw bytes pass through
   untouched, and streaming forwards `aiter_raw()` with zero SSE re-serialisation.
2. **The scheduler is injected, not imported.** `pick_replica` is a
   `Callable[[str, Sequence[ReplicaEndpoint]], ReplicaEndpoint | None]` passed by
   the app layer, keeping gateway and scheduler as independent DAG siblings.
3. **Retry exactly once, only before the first byte.** Connect error / timeout /
   5xx / first-byte-guard timeout re-picks a *different* endpoint once. Once a
   byte reaches the client, a failure truncates the stream — a POST that reached
   the backend is never replayed (matches ADR 000's honest-truncation stance).
4. **Open the stream outside `async with`.** The upstream response and its
   inflight hold are handed to the body generator, which `aclose()`s in a
   `finally`, so the stream lifetime spans the whole response and survives client
   disconnect — avoiding the Starlette/httpx premature-close trap.
5. **Split first-byte from inter-chunk timeout.** httpx `Timeout(connect=2,
   read=15)` guards dialing and inter-chunk gaps; a separate 30s
   `asyncio.wait_for` guards the first token, which legitimately lags.
6. **`now` is injected.** The listed factory signature omitted a clock, but
   `replica_endpoints` and the request log both need one; injecting `now` keeps
   timestamps deterministic under test (see open question).

## Consequences

- The JSONL request log (served vs shed) is the direct source of the on-prem-%
  metric; `agent_id` is attributed to the replica that actually served.
- Live inflight counts (held across streaming) enrich `ReplicaEndpoint.inflight`
  before `pick_replica`, giving load-aware routing fresher than heartbeats.
- A first-byte-guard timeout is retryable (re-picks) because it is detected
  before the `StreamingResponse` is constructed.
