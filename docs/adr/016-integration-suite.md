# ADR 016: End-to-end integration suite (module I3)

Status: accepted · Date: 2026-07-15

## Context

Waves 0–2 built every foundation and composition module behind a
`fallow_protocol` ABC, each unit-tested in isolation. I1 (`create_app`) and I2
(`AgentRuntime` / `resolve_identity` / A5 `CoordinatorClient`) then assembled the
coordinator and the agent. Nothing yet exercises the two assemblies *together*
over the real wire: mint→enroll→heartbeat→submit→lease→complete, churn/eviction,
preemption reaction, and the gateway's streaming proxy + retry. This module (I3)
is that chaos/integration suite. It lives at the top-level `tests/integration/`
(outside every package) so it may import both `fallow_coordinator` and
`fallow_agent` without violating the import-linter DAG (the DAG governs package
source, not tests).

## Decision

- **Real coordinator app, in-process.** Every scenario runs the real
  `create_app(config, now=<FakeClock>, sleep=asyncio.sleep)` inside its own
  lifespan and drives it through `httpx.ASGITransport` — no sockets for the
  coordinator, real `registry`/`queue`/`scheduler`/`chunker`/`gateway`. The ASGI
  transport is pinned to `client=("127.0.0.1", <port>)` so the registration host
  the registry records is loopback, which is where the gateway's replica
  endpoints then point.
- **Real agent uplink (A5 + I2 enrollment).** The agent side is driven through
  the real A5 `CoordinatorClient` (register / heartbeat / poll_work /
  complete_unit / push_event). The lifecycle scenario additionally drives I2's
  `resolve_identity` to exercise the first-run register-and-persist (0600 state
  file) path against the live coordinator. The full `AgentRuntime` event-loop
  machine (poll thread + three async loops + signal handlers) is deliberately
  **not** spun up: its live cadence is wall-clock/thread driven and would make a
  chaos suite non-deterministic. Composing the same public seams by hand (the
  real client, a frozen clock we advance explicitly) keeps every scenario
  deterministic and bounded while still crossing the true HTTP contract.
- **Fakes only at the OS / network edges.** No llama-server, no GPU. The "embed
  worker" is a pure function that turns a lease into a `SUCCEEDED WorkResult`
  (the real A6 worker's only extra behaviour is an HTTP call to llama-server,
  which is exactly the edge we fake). For the gateway a tiny raw-`asyncio` HTTP
  stub role-plays a llama-server replica on `127.0.0.1` with an ephemeral port,
  because the coordinator's *own* httpx client makes a real loopback call to the
  replica (loopback is explicitly allowed for this suite).
- **Determinism via a frozen, advanceable clock.** `create_app`'s injected
  `now` is a `FakeClock`; `sleep` stays `asyncio.sleep`. Frozen time keeps agents
  non-suspect/IDLE for routing; the churn scenario advances the clock past a
  tiny `offline_after_s` so the real eviction loop (tiny `requeue_interval_s`)
  requeues. Client-side polling loops are wrapped in `asyncio.wait_for` so a
  regression fails fast instead of hanging.
- **Gateway retry ordering is made deterministic, not assumed.** The app's
  `pick_replica` (CapabilityScheduler) breaks inflight ties on the
  `"host:port"` string. The retry scenario reserves two equal-length ephemeral
  ports `(dead < live)`, leaves `dead` closed (connection refused) and binds the
  live stub on `live`, so the dead endpoint is always picked first, fails
  pre-first-byte, and the gateway retries onto the live one. The gateway JSONL
  log's `retried=true` is asserted as proof the retry actually happened.

## Scenarios

1. `test_integration_lifecycle` — mint→I2 enroll (0600 persisted)→A5 heartbeat→
   admin `GET /agents` shows the IDLE snapshot with its READY replica.
2. `test_integration_batch` — register+assign embed model → submit a 100-chunk
   JSONL corpus (chunks_per_unit=32 → 4 units) → lease/fetch-input/complete each
   with the fake embed worker → job DONE; resubmit dedups to instant DONE.
3. `test_integration_churn` — agent A leases (attempt 1) then goes silent; clock
   advances past `offline_after_s`; the eviction loop requeues; agent B leases
   (attempt 2) and completes; a late completion from A is a no-op (exactly once).
4. `test_integration_preemption` — `user_returned` event → batch long-poll 204
   immediately (app-layer override); an ACTIVE heartbeat then removes the agent
   from gateway routing (503 SHED, per ADR 014 the gateway reads registry state);
   `user_idle` + IDLE heartbeat restores both paths.
5. `test_integration_gateway` — an SSE stub replica driven via a real READY
   heartbeat: a keyed client streams `/v1/chat/completions` and gets verbatim
   bytes; a mid-stream stub close truncates cleanly; a dead+live replica pair
   proves pre-first-byte retry serves from the live one.
6. `test_integration_auth` — agent routes reject a bad device token; the gateway
   rejects a bad api key; admin rejects a bad admin key.

## Consequences

- The suite crosses the real HTTP/JSON/SSE contract between the two assemblies
  while staying socket-free for the coordinator and deterministic under a frozen
  clock; the whole file set runs in well under the 60s budget with no test sleep
  over 0.5s.
- Because the full `AgentRuntime` loops are not spun, the suite does not cover
  the agent's *internal* loop cadence (that is I2's own unit tests); it covers
  the agent→coordinator wire behaviour those loops produce. This is a deliberate
  determinism/coverage trade, recorded honestly.
- Scenario 4 documents the known ADR-014 gap: the `user_returned` event overlay
  is batch-long-poll-only, so gateway exclusion is driven by the ACTIVE
  heartbeat (which is exactly what the real agent sends within ≤5s of a return).

## Open questions

- None blocking. The gateway-vs-event-overlay asymmetry is an I1 design note
  (ADR 014 open questions: a registry `set_agent_state` would let the gateway
  react on the event too), surfaced here only because scenario 4 must model the
  agent's real ACTIVE heartbeat to exclude a replica from interactive routing.
