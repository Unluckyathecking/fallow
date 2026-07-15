# ADR 000: Architecture baseline (v0.1)

Status: accepted · Date: 2026-07-15

## Decisions

1. **Replication + task distribution, not model sharding.** Every replica is a complete
   model on one machine (llama.cpp / faster-whisper). Autoregressive decoding across
   office networks is latency-fragile and churn-fragile; sharding is out of scope until a
   stable wired subgroup exists (workload class 3, post-v0.1).
2. **Central governance + distributed execution.** One coordinator owns registry, queue,
   scheduler, gateway, auth, audit. Agents hold no policy and initiate every connection
   (registration, heartbeat, long-poll work acquisition, blob pull). The only
   coordinator→agent traffic is proxied inference to replica ports learned from heartbeats.
3. **Instant preemption over polite coexistence.** `psutil.suspend()` of all fallow-owned
   children within 300ms (p99) of user input, escalating to kill for VRAM release. Users
   must never notice Fallow.
4. **Python for v0.1, ports later.** `fallow-protocol` (pydantic + stdlib only) is the
   enforced portability boundary; JSON Schemas are committed and diffed in CI.
5. **SQLite as the only datastore.** WAL-mode SQLite on coordinator-local disk for
   queue/registry/results; `sqlite-vec` for RAG vectors. No Redis, no Celery, no vector DB.
6. **Transport security delegated to the tailnet.** v0.1 must run inside Tailscale (or
   equivalent). Bearer tokens (hashed at rest) for identity; replica ports bind to the
   tailnet interface only. mTLS is v0.2.
7. **Modularity is machine-enforced.** import-linter contracts encode the module DAG;
   cross-module seams are ABCs in `fallow_protocol.interfaces`; every module has its own
   tests and ADR.

## Consequences

- Interactive throughput scales with replica count; single-request latency does not.
- A mid-stream preemption can truncate one interactive response (gateway retries only if
  zero bytes were sent). Accepted for v0.1 and documented honestly.
- Single coordinator is a SPOF; acceptable at ≤50 machines, revisit for HA later.
