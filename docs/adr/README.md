# Architecture Decision Records

Each ADR captures one decision, its context, and its consequences. They are the source
of record; [`docs/architecture.md`](../architecture.md) ties them together into the
system as built. Each record carries its own `Status` line and date: 000-076 and
106 are accepted (027 has no status line), while 077 and the Site Mode series
078-104 are marked proposed. Records 000-017 describe v0.1.0; later ones were written as the system grew.

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
| [031](031-slot-aware-inflight-routing.md) | Slot-aware inflight routing | Publish busy llama-server slots through `ReplicaStatus.inflight` and route with the larger reported or local count. |
| [032](032-rag-vector-store.md) | RAG vector store | Keep fixed-dimension sqlite-vec collections in a versioned sibling `rag.db`. |
| [033](033-rag-ingestion.md) | Fleet RAG ingestion | Run content-addressed document chunks through durable embed jobs and finalize accepted payloads through a vector-store seam. |
| [034](034-rag-query-route.md) | RAG query route | Embed each query on the first healthy fleet replica, then return raw L2-ranked chunks from the collection. |
| [035](035-offline-install-bundle.md) | Verifiable offline install bundle | Stage locked wheels, pinned runtimes, configuration, and optional models behind one complete hash manifest. |
| [036](036-go-schema-codegen.md) | Go schema generation and conformance fixtures | Generate committed Go wire types from JSON Schemas and test both languages against one fixture set. |
| [037](037-go-core-daemon.md) | Go core daemon (heartbeat, idle, preempt, state) | Port the agent's HTTP client, idle detection, preemption state machine, and identity persistence to Go; a live-coordinator interop test proves `omitempty` keeps empty collections from marshaling as `null`. |
| [038](038-go-supervisor-modelcache.md) | Go process supervisor and model cache (E4.3) | Port A3/A4 to Go: build-tagged per-OS suspend (SIGSTOP / NtSuspendProcess), reaper+health goroutines with no leaks, byte-compatible cache layout, Range-resume + sha256 marker-trust. |
| [039](039-capability-aware-assignment.md) | Capability-aware model assignment | A pure fit check compares a manifest's `min_ram_mb` / `min_vram_mb` against an agent's latest snapshot; VRAM is checked against the roomiest single GPU, not the sum. |
| [040](040-macos-idle-ctypes.md) | macOS idle read binds CoreGraphics via ctypes | Bind the idle function through `ctypes` against CoreGraphics with an explicit signature instead of through a pyobjc lazy attribute. |
| [041](041-go-agent-release.md) | Go agent release tooling (E4.5) | Tag-driven GoReleaser builds of `agentctl` per OS, with check-and-snapshot on PRs and a release on tag; the deploy-script install path is recorded in an addendum. |
| [042](042-instant-takedown.md) | Instant user-triggered takedown (reclaim my device) | Reclaim reuses the suspend-first path: `suspend_all()` first for instant relief, then stop the replicas to free RAM and VRAM. |
| [043](043-gateway-request-validation.md) | Gateway request-body validation and upstream-error hygiene | `validate_chat_body` rejects malformed chat bodies at ingress with a client-safe error, and backend 5xx responses are sanitised before leaving the gateway. |
| [044](044-linux-idle-detection.md) | Linux idle detection via XScreenSaver with a headless fallback | Read XScreenSaver `idle` through `ctypes` on an X11 desktop; cover the headless and unavailable cases explicitly rather than assume a desktop. |
| [045](045-go-agent-daemon.md) | Go agent daemon (`agentctl run`) | A `runtime` package composes the Go building blocks into a daemon that mirrors the Python runtime, with a `config` package that reads the shared agent config file. |
| [048](048-auto-model-selection-on-enroll.md) | Automatic device-compatible model selection on enroll | Opt-in, off by default: at enroll the coordinator assigns the largest registered model that fits the declared caps, and never overrides an operator's assignment. |
| [049](049-idle-prediction.md) | Idle-time prediction on top of the idle detector | A new, off-by-default `IdlePredictor` consumes the `IdleDetector` seam without touching the detectors. |
| [050](050-request-eligibility-telemetry.md) | Request-eligibility telemetry (measure before routing) | A pure `classify_eligibility(body)` labels each request (`local_ok` and the other verdicts) for telemetry only, so routing is decided from measurements. |
| [051](051-coordinator-prometheus-metrics.md) | Coordinator Prometheus metrics | `GET /metrics` serves Prometheus text format 0.0.4 behind the same admin bearer authentication as `/v1/admin/*`. |
| [052](052-replica-bind-address-safety.md) | Replica bind-address safety | One validator rejects empty, wildcard and unspecified bind addresses (including IPv4-mapped IPv6), since `llama-server` is unauthenticated. |
| [053](053-rag-retrieval-into-chat.md) | RAG retrieval into chat generation | An optional `rag` object on a chat body makes the gateway embed the last user message, search the collection and prepend the chunks as one system message. |
| [054](054-coordinator-warm-standby.md) | Coordinator warm standby | Warm standby rather than a consensus cluster: the coordinator periodically exports a consistent snapshot of its single SQLite state file to another location. |
| [055](055-scheduler-reliability.md) | Per-agent task-success reliability in scheduling (C7) | `ReliabilityModel` derives each agent's completed-versus-dropped rate from the unit lifecycle log and feeds it into placement. |
| [056](056-speculative-backup-dispatch.md) | Speculative backup dispatch for batch-job tails (C1, C4) | The queue owns a crash-safe backup lease and idempotent completion; the scheduler owns the pure survival decision of when to back up a straggling unit. |
| [057](057-coordinator-promote.md) | Coordinator promote (manual warm-standby failover) | A coordinator-local promote entrypoint validates a standby snapshot, refuses to overwrite a newer `db_path`, installs atomically and clears stale WAL sidecars; manual only. |
| [058](058-relicense-agpl.md) | Relicense from Apache-2.0 to AGPL-3.0-or-later | The whole project moves to `AGPL-3.0-or-later`, with the full licence text and package metadata updated. |
| [059](059-transport-security-reconciliation.md) | Transport-security documentation reconciliation | `SECURITY.md` and `docs/compatibility.md` are brought in line with ADR 052: workers bind the tailnet IP, `llama-server` is unauthenticated, confidentiality comes from the tailnet. |
| [060](060-windows-installer-hardening.md) | Windows agent installer hardening for the school-IT pilot | Surgical hardening of the four `deploy/windows/` scripts, starting with backend detection that never assumes CUDA. |
| [061](061-macos-installer-hardening.md) | Hardening the macOS agent installer for a school-IT pilot | Keep the Python-venv and Go-binary flavours and the dry-run render seam; add backend detection (Metal on Apple Silicon, CPU build on Intel) and further checks. |
| [062](062-one-shot-bootstrap.md) | One-shot bootstrap installer | `deploy/bootstrap.sh` and `bootstrap.ps1` detect, select and delegate to the existing installers; every side effect stays in `install.*`. |
| [063](063-machine-wide-broker-vs-per-user.md) | Machine-wide broker vs per-user agent on shared PCs | For the pilot, one nominated account per machine runs the agent and idle time is harvested while that account is logged in; general multi-user labs are deferred. |
| [065](065-pilot-acceptance-harness.md) | Phase-A pilot acceptance harness | `tests/acceptance/acceptance_matrix.py` holds every Phase-A row as data (CI-asserted or human-run, with command and expected result) and a runner prints it as a checklist. |
| [070](070-moe-fabric-experimental-track.md) | MoE fabric as an isolated experimental track | MoE fabric is pursued alongside the serving path, not in it; complete-model replication stays how Fallow serves models today. |
| [071](071-modelmesh-core.md) | modelmesh core — content-addressed chunks and a signed manifest | A standard-library-only package chunks a model by sha256, describes it in a signed manifest, stores chunks locally and reconstructs the file with full hash verification. |
| [072](072-modelmesh-peer-exchange.md) | modelmesh peer exchange — verified chunk fetch and safe reconstruction | Peers advertise chunk availability, a worker fetches missing chunks from them, and every chunk is checked against the signed manifest on receipt. |
| [073](073-modelmesh-topology.md) | modelmesh bandwidth limiting and topology-aware peer selection | Two inert-until-adopted modules: bandwidth limiting while the user is active, and peer selection that prefers nearby holders. |
| [074](074-modelmesh-coordinator-integration.md) | modelmesh coordinator and agent integration — off by default, blob fallback | The mesh is wired into the coordinator and agent as an additive, off-by-default path; the blob download stays the default and is unchanged when the mesh is off. |
| [075](075-modelmesh-offline-seeding.md) | modelmesh offline seeding — portable bundles and verify-on-import | A bundle is a plain directory with a manifest and chunks that can be carried in and verified on import, for sites with no uplink. |
| [076](076-moe-research-harness.md) | MoE research bench and its isolation | A standalone bench under `experiments/moe/` records raw observations and derives comparable metrics with a pure function, isolated from the serving path. |
| [077](077-distributed-moe-inference-design.md) | Distributed MoE inference, design directions for the research layer | Design directions for the MoE research layer, recorded as proposals rather than a committed build. |
| [078](078-lan-site-mode.md) | Opt-in LAN Site Mode | One pinned-HTTPS coordinator; the Go Windows agent is the sole Site Mode implementation, keeps `llama-server` on loopback and claims work with a held HTTP request. |
| [079](079-lan-site-coordinator.md) | Pinned HTTPS site bootstrap | The opt-in coordinator identity and HTTPS listener, plus a router that mints strict v1 join files through injected enrollment-token storage. |
| [080](080-lan-site-relay.md) | The bounded Site Mode relay | An in-memory claim broker behind the HTTP relay contract, with bounded queues and no FastAPI or gateway coupling. |
| [081](081-lan-site-presence-fencing.md) | Fence stale presence and site routing | User-presence ordering and the Site Mode routing decision become explicit, persisted coordinator state. |
| [082](082-lan-site-routing.md) | Wire Site Mode claims into gateway routing | Mount the Site Mode APIs and route site agents through the relay while direct agents stay on the current HTTP proxy. |
| [083](083-lan-site-cli.md) | Write per-device Site Mode join files | `flw site` requests a distinct join file per device and writes them without exposing their tokens. |
| [084](084-lan-site-go-client.md) | Strict join files and pinned HTTPS in Go | A standalone Go trust boundary that parses a join file, resolves static candidates and dials the coordinator directly with SPKI pinning. |
| [085](085-go-model-reconciliation.md) | Reconcile assigned models into replicas | `Reconciler.Apply` turns `desired_models` into verified loopback replicas by computing additions and removals against the supervisor. |
| [086](086-go-site-claim-runner.md) | Serve Site Mode claims from loopback | A Go claim loop forwards bounded Site Mode work to a READY loopback replica. |
| [087](087-go-site-runtime.md) | Compose Site Mode in the Windows daemon | Pinned enrollment, model reconciliation, presence ordering and inference claims are wired into the production Go daemon. |
| [088](088-windows-site-install.md) | Install and diagnose a Windows Site Mode agent | A per-user install from one join file that leaves the enrollment token out of config, logs and task arguments, plus a read-only doctor. |
| [089](089-lan-site-acceptance.md) | Prove the static LAN Site Mode path | A black-box release gate for the static-address, outbound-only school pilot path. |
| [090](090-lan-site-mdns-advertise.md) | Optionally advertise the Site Mode address | Optional mDNS advertisement from the coordinator, added after the static path is proven. |
| [091](091-lan-site-go-mdns.md) | Resolve optional Site Mode mDNS candidates | A site profile may try mDNS candidates, but trust is never learned from multicast. |
| [092](092-lan-site-discovery-acceptance.md) | Gate discovery and static fallback | Proves optional discovery improves address recovery without becoming a trust or availability dependency. |
| [093](093-lan-site-pilot-runbook.md) | Publish the LAN Site Mode pilot runbook | The tested operator path is published as `docs/lan-site/operator-runbook.md` and the shared docs are reconciled. |
| [094](094-lan-site-fleet-status.md) | Report live Site Mode fleet status | `flw site status` gives one read-only view of every Site Mode agent: enrollment, heartbeat age, presence, availability and replicas. |
| [095](095-lan-site-doctor-clock.md) | Report clock skew in Site Mode doctor | A drifted PC clock is named as a diagnostic before it surfaces as an opaque pinned-TLS failure. |
| [096](096-lan-site-interception-acceptance.md) | Gate Site Mode against TLS interception | An intercepted connection is refused before any secret leaves the agent and is diagnosed clearly. |
| [097](097-go-host-metrics.md) | Probe real hardware in the Go agent | `go-agent/hostinfo` holds every hardware probe: `Caps` at enrollment and a `Sampler` for per-heartbeat CPU, memory and GPU state. |
| [098](098-go-idle-fail-closed.md) | Ship idle detection in the released macOS agent | `Run` samples the idle detector before enrolling and refuses to start if the platform has none, rather than report the machine permanently idle. |
| [099](099-site-desk-bundle.md) | One artifact per Site Mode desk | Each release carries one `fallow-site-agent_<version>_windows_amd64.zip`; a desk needs that plus a join file and no checkout. |
| [100](100-coordinator-systemd-install.md) | The coordinator is a service, not a terminal someone left open | A systemd unit and one install command, `deploy/coordinator/install.sh --ref <tag>`, pinned to a release tag. |
| [101](101-windows-admin-context-install.md) | Install a desk without walking to it | `install.ps1 -User <account>` registers the desk from an admin or SYSTEM context while the agent still runs as an at-logon task in that account's session. |
| [102](102-install-acceptance-ci.md) | Run the desk installers on real hosts | Three CI lanes run the Windows Pester suite, a real Scheduled Task registration and a real `launchctl bootstrap`, each proving one layer. |
| [103](103-hf-model-staging.md) | Stage models from Hugging Face with derived minimums | `flw models pull hf:<owner>/<repo>/<file.gguf>` stages a model and derives `min_ram_mb` from the GGUF header; only the coordinator host contacts huggingface.co. |
| [104](104-identity-revocation.md) | Revoke an enrolled desk and an unused join token | `flw agents revoke` invalidates a desk's device token and `flw enroll revoke` voids an unused enrollment token; both are terminal. |
| [106](106-fit-assignment-sweep.md) | Fit-assignment sweep | `flw assign <model-id> --fit` assigns a model to every live, unassigned agent that can hold it, on operator demand only. |

## LAN Site Mode (078–096)

One opt-in deployment shape, decided across a series rather than a single record:
an on-site coordinator on the LAN, Windows Go agents reaching it over HTTPS pinned
to the coordinator's SubjectPublicKeyInfo, `llama-server` on loopback only, and no
Tailscale or internet anywhere in the path. It is additive — a coordinator without
`[site].enabled` and an agent without `site_join_bundle` keep their existing
explicit-URL and Tailscale behaviour unchanged.

[078](078-lan-site-mode.md) sets the shape and the trust boundary; [079](079-lan-site-coordinator.md)–[082](082-lan-site-routing.md)
build the coordinator side (listener, relay, presence fencing, routing);
[083](083-lan-site-cli.md) adds `flw site`; [084](084-lan-site-go-client.md)–[087](087-go-site-runtime.md)
build the Go agent side; [088](088-windows-site-install.md) installs it on Windows
from a join file; [089](089-lan-site-acceptance.md) is the acceptance suite.
[090](090-lan-site-mdns-advertise.md)–[092](092-lan-site-discovery-acceptance.md)
add optional mDNS as address recovery only, and 092 records the one gap the suite
cannot close: the multicast hop between our own advertiser and our own resolver is
a pilot-day check, not a test. [094](094-lan-site-fleet-status.md) adds the
`flw site status` fleet view, [095](095-lan-site-doctor-clock.md) the `doctor`
clock lane, [096](096-lan-site-interception-acceptance.md) the TLS-interception
rehearsal, and [093](093-lan-site-pilot-runbook.md) publishes the operator path as
[`docs/lan-site/operator-runbook.md`](../lan-site/operator-runbook.md).
[099](099-site-desk-bundle.md) packages that desk install (binary, scripts and a
verified manifest) as one release artifact, so a pilot machine needs no checkout.
[100](100-coordinator-systemd-install.md) does the same job at the other end: the
coordinator installs as a systemd service from a pinned release tag, so the one
machine every desk depends on comes back after a reboot with nobody present.
[101](101-windows-admin-context-install.md) moves the desk registration off the
pilot user's keyboard: `install.ps1 -User <account>` installs from an elevated
admin or SYSTEM context for a nominated account, while the agent keeps running as
an at-logon task in that account's own interactive session.
[102](102-install-acceptance-ci.md) runs those installers on real hosts: the
Windows Pester suite, a real Scheduled Task registration in both contexts, and a
real `launchctl bootstrap`, so the registration layer stops resting on a dry-run
render. What a hosted runner cannot prove (a logon start, EDR, a real LAN) stays
marked and stays manual.
[103](103-hf-model-staging.md) does the same for the model an operator stages:
`flw models pull` takes `hf:<owner>/<repo>/<file.gguf>` or a `--catalog` id,
reads the quantisation out of the GGUF header, sizes `min_ram_mb` off the file
rather than leaving the number that decides placement at `0`, and never guesses a
GPU. Only the coordinator host dials huggingface.co; an air-gapped site keeps the
carry-it-in `flw models register --file` path.
[104](104-identity-revocation.md) closes the gap the runbook had been naming
since the pilot began: an operator can revoke one desk's device token or one
unused join token from the coordinator, and both take effect at once: the
machine leaves routing, its relayed work is dropped, and the desk itself records
why and stays down instead of restarting into a rejected token. Both are terminal
by design; a compromised coordinator key is still a rotation.
[106](106-fit-assignment-sweep.md) turns fleet placement into one command:
`flw assign <model-id> --fit` assigns a model to every live, unassigned agent
that can hold it, reports the rest with reasons, and never overrides an
existing assignment. `flw jobs submit --assign-fit` runs the same sweep right
after queueing a job. A sweep is an operator action each time it happens; the
coordinator still never moves models on its own.
