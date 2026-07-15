# ADR 018: Agent bench hooks — synthetic user return (module A7)

Status: accepted · Date: 2026-07-15

## Context

The Wave-4 research goal is a 3-arm scheduling experiment (single dedicated
machine, round-robin, churn-aware v2) measuring TTFT, tok/s, units/hour, recovery
time, time-to-yield, energy, and %-served-on-prem. To produce comparable numbers
across arms, the experiment must drive *reproducible churn* — a user "returning"
to a benchmark machine on a fixed schedule so the agent yields.

The benchmark machines are headless: there is no keyboard or mouse to press. The
only signal the preemption poll thread (A2) reacts to is a drop in "seconds since
last input" reported by the idle detector (A1). The churn injector (module B2,
built in parallel) needs to fabricate that drop over a stable, out-of-process
interface. `AgentConfig.bench_mode` already exists in the protocol as the intent
flag; this module is the agent-side mechanism.

## Decisions

1. **Inject at the idle-detector seam, not the state machine.** A
   `BenchIdleDetector` decorates any real `IdleDetector`. After
   `simulate_input()` it reports `0` and counts up from an injected monotonic
   clock; the unchanged poll thread and `PreemptController` then yield exactly as
   they would for a real return. The preemption logic is measured *as shipped* —
   no bench-only branch inside the hot path.
2. **Real input always takes precedence.** The wrapper keeps deferring to the
   synthetic value only until the inner detector reports something *smaller* — a
   genuine OS input event that reset the real counter below the synthetic one. At
   that point the injection is cleared and the real reading passes through.
   Injection can never mask an actual user, so the bench surface is safe to leave
   enabled on a machine someone might touch.
3. **Thread-safe by construction.** The poll thread reads `seconds_since_input()`
   while a bench thread calls `simulate_input()`. Injection state (`_injected_at`)
   is guarded by a lock; the inner detector is read outside it (A1 guarantees each
   call is O(microseconds) and thread-safe).
4. **Control surface is stdlib HTTP, no framework.** Import-linter forbids the
   agent from importing `fastapi`/`aiosqlite`. `BenchListener` is a hand-rolled
   `asyncio.start_server` with just enough HTTP/1.1 to serve two routes. The B2
   contract is fixed: `POST /simulate_input` → `204`; `GET /state` →
   `{"state": "idle|active|draining", "idle_s": float}`. Unknown routes are `404`;
   an unparseable request line is `400`.
5. **No auth, off by default, honest about it.** The surface has no
   authentication. It exists only when the operator sets `[bench] enabled = true`
   and binds to the agent's `bind_host` — the existing settings guard still
   forbids `0.0.0.0`, so it is reachable only on loopback or the tailnet, exactly
   like the inference replicas. Enabling it is a deliberate benchmark-time act.
6. **Surgical wiring at the composition root.** Settings gain a frozen `[bench]`
   table (`enabled: bool = false`, `port: int = 9411`). When enabled,
   `AgentAssembly` wraps the idle detector once — so the poll thread, every
   heartbeat, and the final beat all observe the injected value — and constructs
   the listener; `AgentServices` owns its start/stop lifecycle (started last,
   stopped first, before the drain, so no synthetic input arrives mid-shutdown).
7. **Replay-deterministic.** `BenchIdleDetector` takes an injected `monotonic`
   callable (the shared `RuntimeSeams` clock); nothing in this module reads a wall
   clock or unseeded randomness, so bench runs replay identically.

## Consequences

- B2 can drive churn against any agent over one tiny, stable HTTP contract with
  no HID emulation and no OS-specific input injection.
- The measured yield path is the production path; the only added cost when bench
  is disabled is a single unused settings field.
- The listener is intentionally minimal (two routes, no keep-alive, `Connection:
  close`); it is not a general-purpose server and must never grow auth-bearing or
  mutating routes beyond the churn surface.
