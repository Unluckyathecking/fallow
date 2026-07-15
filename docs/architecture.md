# Fallow architecture (as built, v0.1.0)

This document describes the system **as it is implemented and running**, not as
aspiration. Anything not yet built is called out under an explicit *Future* heading.
The source of record for individual decisions is [`docs/adr/`](adr/README.md); this
page ties those decisions together into one picture.

Fallow turns idle desktops and workstations into a centrally governed inference and
batch-processing fabric, without the people using those machines noticing. The two
load-bearing bets — **replication not sharding**, and **instant preemption** — are
recorded in [ADR 000](adr/000-architecture-baseline.md) and were validated on real
hardware (see [Preemption, measured](#preemption-measured)).

---

## 1. Component diagram

One **coordinator** owns all policy (registry, queue, scheduler, gateway, auth,
audit). Many **agents** hold no policy and initiate every connection. The only
coordinator→agent traffic is proxied inference to a replica port the coordinator
learned from a heartbeat ([ADR 000 §2](adr/000-architecture-baseline.md)).

```text
                         inference clients (OpenAI wire protocol)
                                        │  Bearer <client api key>
                                        v
┌───────────────────────────────────────────────────────────────────────────┐
│  COORDINATOR  (one async process, one WAL SQLite file, tailnet-bound)       │
│                                                                             │
│   app  ── FastAPI factory: agent API + admin API + lifespan ───────────┐    │
│    │                                                                   │    │
│    ├── gateway    (C5)  OpenAI-compatible proxy → replica ; gateway.jsonl    │
│    ├── scheduler  (C4)  SchedulerPolicy + dispatch loop (experiment arm)     │
│    ├── modelserve (C3)  Range-capable blob server (model pull)              │
│    ├── registry   (C2)  agents, tokens, model catalogue, liveness  ──┐      │
│    └── queue      (C1)  jobs, content-addressed work units, leases   │      │
│                              one SQLite (WAL) file  ◄────────────────┘      │
│                                                                             │
│   audit sinks:  events.jsonl (agent events)   gateway.jsonl (requests)      │
└───────────────────────────────────────────────────────────────────────────┘
        ▲  register / heartbeat / events / work long-poll / blob pull
        │  (ALL agent-initiated, plain HTTP+JSON, over the tailnet)
        │                         proxied inference  │
        │  ┌──────────────────────────────────────── ┘
        │  v
┌───────────────────────────┐        ┌───────────────────────────┐
│  AGENT  (macOS / Windows)  │        │  AGENT  (…)                │
│                            │        │                            │
│  main (I2) composition root│        │   idle → preempt → supervisor
│   ├ idle      (A1)  10 Hz   │        │   heartbeat, workers, cache │
│   ├ preempt   (A2)  state   │        └───────────────────────────┘
│   ├ supervisor(A3)  children│
│   ├ modelcache(A4)  blobs   │   llama-server / faster-whisper replicas
│   ├ heartbeat (A5)  uplink  │   bind to the tailnet IP ONLY (never 0.0.0.0)
│   └ workers   (A6)  batch   │
└───────────────────────────┘
```

Every replica is a **complete** quantised model on one machine. Interactive
throughput scales with replica count; single-request latency does not
([ADR 000](adr/000-architecture-baseline.md), consequence 1).

---

## 2. Request flows

### 2.1 Interactive stream (gateway → replica)

An OpenAI-compatible client hits the gateway ([ADR 012](adr/012-gateway.md)). The
gateway authenticates the client key, parses **only** `model` / `stream` /
`prompt_chars` from the body, and forwards the raw bytes verbatim.

```text
client ──POST /v1/chat/completions (Bearer <client key>)──> gateway
  gateway: auth → parse model → pick_replica(model, live replicas)
    ├─ no replica available → bounded FIFO wait (10 s default)
    │     ├─ replica returns ............................ proxy request
    │     └─ timeout or overflow ....................... 503, log status=shed
    ├─ replica reachable → proxy request, stream aiter_raw() back
    │     first-byte + inter-chunk deadlines via wait_for (app-level)
    │     httpx read timeout is a backstop ABOVE those guards (see live fix)
    │     └─ success → stream SSE to client ............. log status=served
    └─ replica unreachable, 0 bytes sent → retry once
          still failing .................................. 502, log status=error
```

Every request emits one immutable `GatewayLogEntry` to `gateway.jsonl`
(`gateway/logentry.py`): `client_key_name`, `model_id`, `agent_id`, `t_submit`,
`t_first_byte`, `t_done`, `status` (`served` | `shed` | `error`), `retried`,
`prompt_chars`, `waited_ms`. That log is the interactive dataset for the scheduling study; the
`served : (served + shed + error)` ratio is the *% served on-prem* metric.

Replica selection is `SchedulerPolicy.pick_replica`: least-inflight first, ties
broken on `host:port` — pure and deterministic, so arms are swappable.

### 2.2 Batch unit lifecycle (submit → lease → complete)

Batch work is **pull-based**: the coordinator never dials an agent to hand out work
([ADR 000 §2](adr/000-architecture-baseline.md), [ADR 011](adr/011-scheduler-v1.md)).

```text
operator ──flw jobs submit──> admin API
  coordinator splits the job into content-addressed work units:
     work_unit_id = sha256(job_id ‖ idx ‖ input_hash)          [ADR 005]
     any unit whose id already has a stored result → DONE now (free dedup)

idle agent ──long-poll /work (device token)──> queue.lease_next(agent, models)
  scheduler.select_agent gates eligibility; queue atomically leases one unit
     → WorkUnitLease{ input_url, lease_expires, attempt }
  agent fetches input_url, runs the local replica (embed / transcribe),
     uploads result blob, ──POST result──> queue.complete_unit (INSERT OR IGNORE)

background loop: queue.requeue_expired()  → expired leases requeue, attempts+1,
                                            DEAD after the retry budget
                 registry offline agent   → queue.requeue_agent(agent_id)
```

Units are idempotent and content-addressed, so duplicate or late completions and
re-submits are harmless by construction. A job reaches `DONE` when every unit is
`DONE` or `DEAD`; the job proceeds without dead units.

### 2.3 Preemption sequence (user returns → yield)

The agent's promise is that **users never notice Fallow**
([ADR 000 §3](adr/000-architecture-baseline.md), [ADR 002](adr/002-preemption.md)).
The yield decision is a tiny synchronous state machine on a **dedicated OS thread**
(not asyncio — no coroutine may delay a suspend), driven by a ~10 Hz idle poll.

```text
poll thread (every poll_interval_ms, default 100):
   idle_s = IdleDetector.seconds_since_input()      # µs-cost, never blocks/spawns
   Preemptor.on_poll(idle_s, monotonic_now):

     IDLE ──fresh input (idle_s < poll interval, or idle_s dropped)──> ACTIVE
        1. supervisor.suspend_all()   ← FIRST side effect (psutil.suspend all children)
        2. yield_ms = monotonic_now_after − monotonic_now
        3. emit(USER_RETURNED, detail={"yield_ms": "<ms>"})   → events.jsonl
        (suspended GPU replica → killed after vram_evict_after_s to free VRAM)

     ACTIVE ──idle_s ≥ idle_threshold_s (default 120)──> IDLE
        supervisor.resume_all(); emit(USER_IDLE); replicas rejoin routing
```

The suspend call is the *first* side effect on return, before the latency read and
before `emit`, so nothing races ahead of releasing the machine to the user. State
transitions are additionally pushed to the coordinator immediately as an
`AgentEvent` (routing must never wait for the next ~5 s heartbeat).

#### Preemption, measured

Validated on a two-machine fleet (MacBook Air, Apple Silicon; Windows 11 / RTX 3070),
2026-07-15 — raw JSON in [`experiments/spikes/RESULTS.md`](../experiments/spikes/RESULTS.md):

| Metric | Target | Mac | Windows/RTX |
| --- | --- | --- | --- |
| End-to-end yield `total_ms` p99, **full CPU load** | < 300 ms | **103.1 ms** | **116.3 ms** |
| `suspend` syscall p99 (raw psutil) | < 15 ms | 2.44 ms | 0.16 ms |
| **Real production yield** (Mac user physically returned mid-session) | — | **1.268 ms** | — |
| CUDA suspend/resume cycles corrupting output | 0 | — | **0 / 500** (VRAM 1558→1563 MB) |

End-to-end yield is **2.6× inside** the 300 ms budget and is dominated by the poll
phase (`detect_ms ≈ total_ms`); the suspend syscall itself is sub-3 ms. The single
lever, if ever needed, is poll cadence — not the suspend mechanism. The 1.268 ms
figure is the unstaged real event captured live from `events.jsonl`
(`{"kind":"user_returned","detail":{"yield_ms":"1.268"}}`); the replica auto-resumed
after the 120 s idle threshold. In the same demo the gateway served a **warm TTFT of
222 ms** end-to-end (client → gateway → PC replica over the tailnet), and a
hard-killed PC agent (`taskkill /F`) caused **zero failed client requests** — every
request routed to the surviving Mac replica across the failure and the 45 s offline
window.

---

## 3. Module DAG (import-linter enforced)

Modularity is machine-enforced: `uv run lint-imports` runs in CI against the
contracts in `pyproject.toml` (`[tool.importlinter]`). Cross-module seams are ABCs in
`fallow_protocol.interfaces`; modules depend on those abstractions, never on each
other's concrete classes.

```text
                    fallow_protocol   (pydantic + stdlib ONLY — portability boundary)
                          ▲   ▲   ▲
       ┌──────────────────┘   │   └──────────────────┐
 fallow_coordinator      fallow_agent            fallow_bench
 (imports never          (never imports          (imports ONLY
  reach agent/cli/bench)  server code:            fallow_protocol
                          no fastapi/aiosqlite)    + 3rd party)
 fallow_cli ─► fallow_protocol only (+ typer/rich/httpx)
```

Enforced contracts (mirrors `[tool.importlinter.contracts]`):

1. **`fallow_protocol` is self-contained.** It may import *nothing* first-party and
   none of `fastapi`, `httpx`, `psutil`, `aiosqlite`, `typer`. This is the boundary
   for a future Go/Rust port.
2. **Coordinator and agent never import each other** (nor `fallow_cli` / `fallow_bench`).
3. **Agent never imports server-side code** — forbidden: `fallow_coordinator`,
   `fallow_cli`, `fallow_bench`, `fastapi`, `aiosqlite`. The agent hot path stays free
   of the async server stack.
4. **`fallow_bench` imports only `fallow_protocol`** (+ third-party). The bench harness
   must never reach into coordinator or agent internals, so replay stays deterministic.
5. **Coordinator internal layers** (`containers = ["fallow_coordinator"]`):

   ```text
   app  ▸  gateway | scheduler | modelserve  ▸  registry | queue
   ```

   Higher layers may import lower; siblings in a tier may not import each other.
6. **Agent internal layers** (`containers = ["fallow_agent"]`):

   ```text
   main  ▸  heartbeat | workers  ▸  idle | preempt | supervisor | modelcache
   ```

Both layer contracts are `exhaustive = false` (helper packages may sit outside a
tier). The integration suite lives at top-level `tests/integration/` — outside every
package — so it may import both `fallow_coordinator` and `fallow_agent` without
violating the DAG, which governs package **source**, not tests
([ADR 016](adr/016-integration-suite.md)).

---

## 4. Protocol versioning and schema-drift CI

`fallow_protocol` is the wire contract shared by coordinator and agent. It carries a
single integer `PROTOCOL_VERSION` (currently **1**) plus the package `__version__`
(`0.1.0`).

- **Version is exchanged, not negotiated.** The agent sends `protocol_version` at
  registration and in every heartbeat. A mismatch is **rejected at registration
  time** — there is no in-place protocol negotiation in v0.1
  (`fallow_protocol/version.py`). `PROTOCOL_VERSION` bumps on any breaking change to a
  wire type.
- **Schemas are committed and diffed in CI.** Every concrete wire type is listed in
  `WIRE_TYPES` and exported to JSON Schema under [`schemas/`](../schemas/) by
  `python -m fallow_protocol.export_schemas`. CI regenerates the schemas and runs
  `git diff --exit-code -- schemas/`, so **any unintended change to a wire type fails
  the build** until the committed schema and (implicitly) the version are updated.
- **Frozen models.** Wire types subclass `FallowModel` and are frozen — messages are
  immutable values, not mutable state. Connection state (agent id, bearer token) lives
  on client objects, never on wire messages.

The stability and compatibility policies live in
[`docs/api-stability.md`](api-stability.md) and [`docs/compatibility.md`](compatibility.md).

---

## 5. Trust model

### 5.1 Transport: delegated to the tailnet

v0.1 has **no transport encryption of its own** — it delegates that to Tailscale (or
an equivalent tailnet), which is **mandatory**
([ADR 000 §6](adr/000-architecture-baseline.md)). Consequences enforced in code:

- The coordinator admin and gateway APIs are reached over the coordinator's tailnet IP.
- Each agent's llama-server replica ports **bind to the agent's tailnet IP only**; the
  supervisor config **rejects `0.0.0.0`** outright, because `llama-server` has no
  authentication of its own and an all-interfaces bind would expose an open inference
  endpoint on the office LAN (`deploy/README.md` §1.1).

### 5.2 Identity: three bearer-token types + one admin key

All bearers are `secrets.token_urlsafe(32)` strings, returned to the holder **once**
and stored **only** as a sha256 hex digest; verification re-hashes and compares in
constant time with `hmac.compare_digest` (`registry/tokens.py`,
[ADR 006](adr/006-registry-auth.md)). The three registry-minted token types:

| Token | Held by | Grants | Minted at |
| --- | --- | --- | --- |
| **Enrollment token** | an operator, handed to a new agent | one-time registration | operator, via `flw` |
| **Device token** | one agent | heartbeat, events, work long-poll, blob pull | registration response |
| **Client API key** | an inference client | the OpenAI-compatible gateway | operator, via `flw` |

Separately, a single static **admin key** (`CoordinatorConfig.admin_key`, `Bearer`
on `/v1/admin/*`) authorises the operator `flw` CLI. It is a shared config secret, not
a per-identity registry token.

### 5.3 What a compromised worker can and cannot do

A worker (agent) holds no policy and initiates every connection, which bounds the
blast radius of a compromised or malicious agent:

**Can:**
- Act as itself using its own device token (heartbeat, long-poll work, pull blobs it is
  assigned, submit results for units it leased).
- Return wrong results for units it legitimately leased. Units are idempotent and
  content-addressed but results are **not** re-verified against ground truth in v0.1 —
  a lying worker can corrupt *its own* unit outputs (accepted; see *Future*).

**Cannot:**
- Reach another agent — there is **no** agent↔agent traffic; agents never dial each
  other, and the coordinator only dials a replica port to proxy inference.
- Impersonate the coordinator or another agent (tokens are per-identity, hashed at rest).
- Serve a corrupt model blob to itself undetected: the agent verifies **sha256 before
  first use** (`ModelStore`, [ADR 004](adr/004-model-cache.md) / [ADR 007](adr/007-model-serving.md)).
  A lying admin that registers a manifest whose bytes don't match only causes a **failed
  replica launch**, not silent corruption ([ADR 014](adr/014-coordinator-app.md) consequences).
- Escalate on the user's machine beyond its own child processes: the supervisor owns only
  fallow-launched children and suspends/kills exactly those.

**Residual centralised risks (documented, not eliminated in v0.1):**
- The coordinator is a **single point of failure** and single SQLite writer — accepted at
  ≤ 50 machines ([ADR 000 §6](adr/000-architecture-baseline.md)).
- A mid-stream preemption can truncate **one** interactive response; the gateway retries
  only when **zero** bytes were sent.

---

## 6. Composition roots and entrypoints

- **Coordinator:** `python -m fallow_coordinator serve --config <coordinator.toml>` —
  `create_app(config) -> FastAPI` builds every collaborator over one SQLite file with the
  **config-selected scheduler policy** (`scheduler = "capability" | "roundrobin" |
  "churn_v2"`, chosen by `_build_policy`), an async lifespan running the requeue/dispatch
  background loops ([ADR 014](adr/014-coordinator-app.md)). Config is a single frozen
  `CoordinatorConfig` loaded from TOML and overlaid with `FALLOW_COORD_*` env vars. The
  three scheduler policies are the experiment arms — see
  [`docs/experiment.md`](experiment.md).
- **Agent:** `python -m fallow_agent run` — `AgentAssembly.build` wires idle → preempt →
  supervisor → modelcache → heartbeat → workers into a supervised daemon that shuts down
  without a trace ([ADR 015](adr/015-agent-runtime.md)).
- **Operator CLI:** `flw` — enroll agents, mint client keys, register/pull models, assign
  models, submit/inspect jobs ([ADR 013](adr/013-cli-admin-api.md),
  [`docs/admin-api.md`](admin-api.md)).

Deployment (binary staging, per-machine service install, and why agents must run in the
logged-in GUI session) is covered in [`deploy/README.md`](../deploy/README.md) and
[ADR 017](adr/017-deploy.md).

---

## Future

Aspirational; **not** in v0.1.0. Tracked in [`ROADMAP.md`](../ROADMAP.md).

- **mTLS / workload identity** so the system does not rely solely on the tailnet for
  transport security ([ADR 000 §6](adr/000-architecture-baseline.md), v0.2).
- **Result verification / attestation** to shrink the "lying worker corrupts its own
  outputs" blast radius.
- **Registry `set_agent_state`** so the *gateway* interactive path reacts to
  `user_returned` on the event rather than the next heartbeat — today only the batch
  long-poll path has that immediacy via an app-layer overlay
  ([ADR 014 open questions](adr/014-coordinator-app.md)).
- **HA coordinator** beyond the single-writer SQLite SPOF.
- **Model sharding** (workload class 3) once a stable wired subgroup exists.
