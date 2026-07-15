# Fallow

**An opportunistic private AI compute layer for organisations.**

Fallow turns an organisation's existing fleet of desktops, laptops and workstations into a
private, centrally governed AI inference and batch-processing fabric — using spare resources
**without disrupting the people using the machines**.

- **Replication, not sharding**: each capable machine runs a *complete* small quantised model
  (via llama.cpp) or a specialist worker (embeddings, speech-to-text). Requests route to
  whichever replica is available. No model is ever split across unreliable machines.
- **Instant preemption**: the moment a user touches their machine, all Fallow workloads yield
  — target p99 **< 300 ms**. The fleet only ever uses genuinely idle capacity.
- **Central governance, distributed execution**: one coordinator owns the device registry,
  model registry, job queue, capability-aware scheduler, audit log, and an
  OpenAI-compatible gateway (`/v1/chat/completions`, `/v1/embeddings`). Workers hold no policy.
- **Local-first**: prompts, documents and model weights stay inside the organisation.
  Worker machines need zero internet egress.

## Status

Pre-release. v0.1 targets a two-machine dev fleet (Apple Silicon Mac + Windows/RTX PC over
Tailscale) and a school-lab pilot: private RAG search over policy documents, an internal
coding assistant, and overnight document indexing.

## Architecture

```
                    internal apps (Open WebUI, RAG search, CLI)
                                     │
                     OpenAI-compatible gateway  ──  coordinator
                     (auth · routing · policy)      (registry · queue · scheduler · audit)
                        ┌────────────┼────────────┐
                        │            │            │
                    agent         agent         agent          ← per-machine daemon
                    llama.cpp     llama.cpp     embeddings     ← complete replicas
                    (idle PC)     (idle PC)     (idle PC)
```

Packages (uv workspace):

| Package | Role |
|---|---|
| `fallow-protocol` | Wire types + interface ABCs. Depends on pydantic + stdlib **only** — the portability contract for a future Go/Rust port. |
| `fallow-coordinator` | FastAPI server: registry, auth, queue, scheduler, model distribution, OpenAI gateway. |
| `fallow-agent` | Per-machine daemon: idle detection, preemption, inference-process supervision, batch workers. |
| `fallow-cli` | `flw` — enroll, models, jobs, status, bench. |
| `fallow-bench` | Experiment harness: workload generator, churn injector, metrics analysis. |

## Non-goals (v0.1)

No model sharding or distributed inference; no fine-tuning; no mTLS (deployment requires a
private overlay network such as Tailscale); no rate limiting or multi-tenancy; no HA
coordinator; no containers on workers; no custom inference engine. **No high-risk uses under
the EU AI Act** — nothing touching grading, admissions, behaviour monitoring or profiling
(see `docs/ai-act-scoping.md`).

## Development

```bash
uv sync                    # install everything (workspace)
uv run pytest              # tests
uv run ruff check .        # lint
uv run mypy                # strict type check
uv run lint-imports        # enforce the module dependency DAG
```

## License

Apache-2.0
