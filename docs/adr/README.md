# Architecture Decision Records

Each ADR captures one decision, its context, and its consequences. They are the source
of record; [`docs/architecture.md`](../architecture.md) ties them together into the
system as built. All records below are **accepted**, dated 2026-07-15, and describe
v0.1.0.

Numbers map to build waves: `A*` = agent modules, `C*` = coordinator modules, `S1` = the
risk spikes, `I*`/`L1` = composition and tooling.

| # | Title | Decision (one line) |
| --- | --- | --- |
| [000](000-architecture-baseline.md) | Architecture baseline (v0.1) | Replication + central governance + instant preemption; Python + pydantic portability boundary; WAL SQLite only; security delegated to the tailnet; modularity machine-enforced. |
| [001](001-idle-detection.md) | Idle detection (A1) | One µs-cost `IdleDetector` per OS behind a `create_idle_detector()` factory dispatching on `sys.platform` (Windows `GetLastInputInfo`, macOS Quartz, Linux). |
| [002](002-preemption.md) | Preemption state machine (A2) | Synchronous yield state machine on a dedicated OS thread; `suspend_all()` is the first side effect on user return; deterministic and un-wedgeable. |
| [003](003-process-supervisor.md) | Inference process supervisor (A3) | `ChildProcessSupervisor` owns every fallow child with injected seams; hot-path suspend never blocks/spawns; slow readiness/crash checks kept off the hot path. |
| [004](004-model-cache.md) | Agent model cache (A4) | `HttpModelStore` with resumable pulls; marker-based presence (`<file>.sha256`) instead of rehashing multi-GB blobs each heartbeat. |
| [005](005-queue-store.md) | Durable job/work-unit queue (C1) | `SqliteQueueStore` (aiosqlite, WAL, hand-written SQL) with injected clock; content-addressed idempotent units; leasing, retries, dedup. |
| [006](006-registry-auth.md) | Registry & auth store (C2) | One `SqliteRegistry` (WAL, `registry_`-prefixed tables) for agents, hashed bearer tokens, model catalogue, assignments and liveness maths. |
| [007](007-model-serving.md) | Model blob serving (C3) | `create_modelserve_router(registry)` FastAPI router over a narrow `BlobRegistry` protocol; HTTP Range, no whole-file buffering; agent verifies sha256 before use. |
| [008](008-spike-plan.md) | Risk-spike plan (S1) | Four throwaway spikes (suspend latency, CUDA suspend cycles, load times, proxy overhead) to measure ADR-000's load-bearing bets before building on them. |
| [009](009-heartbeat-client.md) | Coordinator client + heartbeat loop (A5) | One typed `CoordinatorClient` over an injected `httpx.AsyncClient`; three-way error taxonomy (auth/transient/protocol); non-blocking event emission. |
| [010](010-batch-workers.md) | Batch workers (A6) | Thin `Worker` protocol (`embed`, `transcribe`) that is pure bytes→bytes over a local replica; concurrency/retries/accounting live in the runner above. |
| [011](011-scheduler-v1.md) | Scheduler v1 policies + dispatch (C4) | Two pure, swappable arms: `CapabilityScheduler` (warm-replica/GPU/RAM ranked) and `RoundRobinScheduler`, plus a PULL-based dispatch loop. |
| [012](012-gateway.md) | OpenAI-compatible gateway (C5) | Parse only `model`, forward the body verbatim, stream `aiter_raw()`; injected scheduler; one `GatewayLogEntry` per request for the on-prem metric. |
| [013](013-cli-admin-api.md) | `flw` CLI + admin API contract (L1) | The CLI defines the admin contract in `docs/admin-api.md`; `AdminClient` wraps an injected `httpx.Client`; depends on `fallow_protocol` + typer/rich/httpx only. |
| [014](014-coordinator-app.md) | Coordinator app factory (I1) | `create_app(config) -> FastAPI` composes registry/queue/scheduler/gateway/modelserve over one SQLite file; sync construction, async lifespan; `serve` entrypoint. |
| [015](015-agent-runtime.md) | Agent runtime composition root (I2) | `AgentAssembly.build` is the one place agent modules are wired into a supervised daemon that resolves identity, reconciles replicas, runs work, and exits cleanly. |
| [016](016-integration-suite.md) | End-to-end integration suite (I3) | A top-level `tests/integration/` chaos suite exercising both assemblies over the real wire (enroll→lease→complete, churn, eviction, preemption, gateway retry). |
| [017](017-deploy.md) | Deployment: binary staging + service install (I4) | Stage a pinned/checksummed `llama.cpp`; install agents in the **logged-in GUI session** (LaunchAgent / at-logon Scheduled Task) because idle detection needs it. |
| 018 | [Agent bench hooks](018-bench-hooks.md) | Bench-mode idle-injection listener (`/simulate_input`, `/state`) wrapping the OS idle detector; off by default. |
| [019](019-bench-workload.md) | Bench workload generator (B1) | Open-loop, seeded arrival schedule precomputed from the seed (`random.Random`); requests fire at fixed offsets with no back-pressure, so a slow arm's queueing is measured, not hidden. |
| [020](020-bench-churn.md) | Bench churn injector (B2) | One-RNG seeded schedule of per-agent idle→active renewal processes emitting user-return taps (kill/net-drop opt-in); scripted override; the injector owns no time (injected clock/sleep). |
| [021](021-bench-analysis.md) | Bench metrics analysis (B3) | Layer-clean structural JSON parsing (`fallow_protocol` + pandas/numpy only); pure total loaders and `frame → float \| None` metrics; one numpy linear-interpolation percentile; deterministic, no wall-clock. |
| 022 | Churn-aware scheduler v2 (arm c v2) | `ChurnAwareScheduler` ranks placement by an empirical idle-survival model built from `events.jsonl`; live model refresh deferred. [022-scheduler-v2.md](022-scheduler-v2.md). |
| [023](023-test-imports.md) | Test import hygiene | Test filenames are globally unique, `conftest.py` files contain fixtures only, and shared helpers use directory-specific module names. |
| [024](024-unit-lifecycle-log.md) | Unit lifecycle log and experiment time | Queue transitions are appended after commit, and recovery inputs use UTC epoch seconds. |
| [025](025-result-payloads.md) | Attempt-bound result payloads | Stream result bytes into content-addressed storage and accept completion only for the matching lease attempt and binding. |
| [026](026-experiment-orchestration.md) | Canonical experiment orchestration | Fix the paired nine-run plan, isolate every run directory, and verify the same contract through a fast smoke path. |
| [027](027-gated-benchmark-fleet.md) | Gated benchmark fleet | Allow constant idle only behind bench mode, keep rendered fleet bundles secret-free, and leave provisioning behind an explicit maintainer decision. |
| [028](028-gateway-session-affinity.md) | Gateway session affinity | Keep a bounded TTL/LRU map, reuse only healthy endpoints, and return misses to the scheduler. |
| [029](029-interactive-admission.md) | Interactive admission queue | Wait briefly for a healthy replica in a bounded FIFO queue before shedding interactive traffic. |
| [030](030-api-key-quotas.md) | API key request quotas | Enforce optional token-bucket RPM and UTC-day limits, with bounded-loss registry snapshots. |
| [032](032-rag-vector-store.md) | RAG vector store | Keep fixed-dimension sqlite-vec collections in a versioned sibling `rag.db`. |
| [033](033-rag-ingestion.md) | Fleet RAG ingestion | Run content-addressed document chunks through durable embed jobs and finalize accepted payloads through a vector-store seam. |
| [036](036-go-schema-codegen.md) | Go schema generation and conformance fixtures | Generate committed Go wire types from JSON Schemas and test both languages against one fixture set. |
| [037](037-go-core-daemon.md) | Go core daemon (heartbeat, idle, preempt, state) | Port the agent's HTTP client, idle detection, preemption state machine, and identity persistence to Go; a live-coordinator interop test proves `omitempty` keeps empty collections from marshaling as `null`. |
| [038](038-go-supervisor-modelcache.md) | Go process supervisor and model cache (E4.3) | Port A3/A4 to Go: build-tagged per-OS suspend (SIGSTOP / NtSuspendProcess), reaper+health goroutines with no leaks, byte-compatible cache layout, Range-resume + sha256 marker-trust. |

> **Scope of this index.** ADRs **000 through 030**, **032–033**, and **036–038** are accepted and present.
