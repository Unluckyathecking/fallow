# ADR 014: Coordinator app factory (module I1)

Status: accepted · Date: 2026-07-15

## Context

Waves 0–2 delivered every coordinator foundation module in isolation: registry
(C2), queue (C1), scheduler (C4), gateway (C5), modelserve (C3), each with its
own tests and ADR, each composed only through `fallow_protocol` and its own
public API. Nothing yet assembles them into a running HTTP service. This module
(I1) is that assembly: `create_app(config) -> FastAPI`, the agent-facing wire API
the A5 `CoordinatorClient` dials, the admin API the `flw` CLI (L1) is already
tested against (`docs/admin-api.md`), job submission + chunking, and the
`python -m fallow_coordinator serve` entrypoint.

## Decision

- **Synchronous construction, async lifespan.** `create_app` builds every
  collaborator (both SQLite stores, the `CapabilityScheduler`, the shared
  `httpx.AsyncClient`, the events writer) synchronously so all four routers mount
  before serving. The lifespan then `open()`/`init()`s the stores and starts the
  background loops; shutdown stops the loops and closes the stores and client.
  This sidesteps the "router needs a live store that only exists after an async
  open" ordering trap without deferring route registration.
- **One SQLite file, two stores.** `SqliteRegistry` and `SqliteQueueStore` share
  `db_path`; the registry's tables are `registry_`-prefixed so there is no
  collision. Each initialises its own schema on `open()`/`init()`.
- **Injected clock and sleeper.** `create_app(config, *, now, sleep, token_factory)`
  threads a clock and sleeper through the registry, queue, dispatch loop,
  eviction loop, gateway, and long-poll deadline maths, so every time-dependent
  behaviour is deterministic under test. Production defaults are
  `datetime.now(UTC)` and `asyncio.sleep`.
- **Verbatim agent wire contract.** Paths and status codes match
  `fallow_agent.heartbeat.constants` exactly: register `201`, heartbeat `200`,
  events `202`, work `200`-with-lease or `204`, result `200`, plus
  `GET /v1/work_units/{id}/input`. Device-token auth guards all but registration;
  a bad/used enrollment token is `401`, a protocol mismatch `409`.
- **Long-poll is pull-based.** `GET .../work` loops until `min(timeout,
  long_poll_max_s)`: it builds the agent's snapshot, takes the `READY` replica
  `model_ids` from its latest heartbeat, gates them through
  `scheduler.select_for_poll` (IDLE + non-suspect), and calls `queue.lease_next`;
  otherwise it sleeps `poll_sleep_s` and retries, returning `204` at the deadline.
- **Content-addressed chunking (ADR 005).** `POST /v1/admin/jobs` splits a
  `JobSubmit` at submit time. `embed` accepts a `.jsonl` corpus or a directory,
  grouped `chunks_per_unit` per unit; `transcribe` accepts a directory of
  segmented audio, one unit per file. A unit's input is written into
  `unit_input_dir` keyed by the sha256 of its bytes; `work_unit_id =
  sha256(model_id ‖ chunker_version ‖ input_hash)`, content-derived: a
  re-submit produces identical ids and the queue's dedup short-circuits to `DONE`.
  `WorkUnitSpec.input_ref` is the content hash the input route resolves. Unknown
  `payload_ref` shapes are rejected `422`.
- **Admin bodies are duplicated, not imported.** The coordinator must not depend
  on `fallow_cli` (import-linter DAG), so `ApiKeyRequest` / `ModelRegisterRequest`
  / `AssignmentRequest` are re-declared as frozen `FallowModel`s in
  `app/admin_models.py`, mirroring `fallow_cli.models`. `docs/admin-api.md` + the
  committed JSON Schemas are the shared contract that guards against drift.
- **Model registration trusts the manifest.** `POST /v1/admin/models` validates
  only that `blob_path` exists; it does **not** re-hash the blob. Streaming a
  multi-GB gguf to verify `sha256` on the request path would block the event loop
  for seconds. The CLI computes `sha256`/`size_bytes` locally at register time;
  agents re-verify on pull (C3/C4). Server-side re-hash is deferred to an async
  background ingest (v0.2).
- **Gateway routing sees live load.** The gateway is mounted with an
  inflight-enriched `pick_replica`: it merges the router's own `get_inflight()`
  counts into `ReplicaEndpoint.inflight`, then delegates to
  `CapabilityScheduler().pick_replica`. (The gateway service already enriches with
  the same tracker internally, so the app-layer merge is idempotent; it is kept so
  the app owns the policy wiring and the seam is explicit.)
- **Event-driven state overlay.** `POST .../events` appends the event to the JSONL
  file under a single-writer lock and, for `user_returned` / `user_idle`, records
  an app-layer state override so batch routing reacts immediately rather than
  waiting for the next heartbeat (the registry exposes no direct state setter;
  see open questions).

## Consequences

- The coordinator is a single async process over one SQLite file: a SPOF and a
  single writer, accepted at ≤50 machines (ADR 000 §6).
- The event-state overlay makes *batch* long-poll routing react to
  `user_returned` instantly, but the *gateway* interactive path still reads agent
  state from the registry (it catches up on the next ≤5s heartbeat). Closing that
  gap cleanly wants a registry state setter.
- Trusting the manifest `sha256` means a lying admin client could register a blob
  whose bytes do not match; agents still detect this on pull (verify-before-use),
  so the blast radius is a failed replica launch, not silent corruption.

## Open questions

- `SqliteRegistry` exposes no direct agent-state setter, so immediate reaction to
  `user_returned`/`user_idle` is implemented as an app-layer overlay applied only
  on the batch long-poll path (`registry/sqlite_registry.py:190`,
  `record_heartbeat` is the only state mutator). A first-class
  `set_agent_state(agent_id, state)` would let both batch and gateway routing
  react on the event.
- `registry.set_assignments(agent_id, model_ids)` is per-agent, but the admin
  contract `PUT /assignments {model_id, agent_ids}` is per-model exact-replace.
  The app recomputes it by unioning online snapshots + offline ids + targets and
  editing each agent's set (`registry/sqlite_registry.py:313`). A native
  `set_model_agents(model_id, agent_ids)` would be atomic and cheaper.
- The deploy README documents `uvicorn fallow_coordinator.app:build_app --factory`
  (`deploy/README.md:119`); this module provides `build_app()` (reads
  `FLW_COORDINATOR_CONFIG` → `~/.fallow/coordinator.toml`) to satisfy it, in
  addition to the task-specified `create_app(config)` and `serve` subcommand.
