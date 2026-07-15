# fallow_agent.preempt — preemption state machine

Fallow's signature feature: when the user touches an idle machine, every
fallow-owned inference process is suspended within the p99 300 ms budget, and
work only resumes after the user has genuinely gone away again.

## Public API

- **`PreemptController`** — implements `fallow_protocol.interfaces.Preemptor`.
  Constructed with injected collaborators:

  ```python
  PreemptController(
      supervisor: ProcessSupervisor,
      sink: EventSink,
      config: AgentConfig,
      agent_id: str,
      monotonic: Callable[[], float] = time.monotonic,
      now: Callable[[], datetime] = <utc now>,
  )
  ```

  - `on_poll(idle_s, monotonic_now) -> AgentState` — advance one tick.
  - `state` — current `AgentState`.
  - `drain()` — enter terminal `DRAINING` (agent shutdown); idempotent.

- **`PollLoop`** — a plain `threading.Thread` (not asyncio) that reads
  `IdleDetector.seconds_since_input()` then calls `Preemptor.on_poll(...)` every
  `config.poll_interval_ms`. Use `start()` / `stop(timeout)`.

## State machine

| From | Trigger | To | Actions (in order) |
|------|---------|----|--------------------|
| IDLE | fresh input (`idle_s < poll_interval_s`, or `idle_s < prev idle_s`) | ACTIVE | `suspend_all()` **first**, measure yield, `emit(USER_RETURNED, yield_ms)` |
| ACTIVE | held ACTIVE ≥ `vram_evict_after_s` | ACTIVE | `stop_replica()` once for each replica still `SUSPENDED` |
| ACTIVE | `idle_s ≥ idle_threshold_s` | IDLE | `resume_all()`, `emit(USER_IDLE)` |
| any | `drain()` | DRAINING | `emit(AGENT_STOPPING)` (terminal) |

## Invariants

- **Suspend is the first side effect** on a user-return tick. Nothing (not even
  the yield-latency read) runs before `supervisor.suspend_all()`.
- **`emit` never blocks** (EventSink contract) and neither do the supervisor
  hot-path calls; `on_poll` is safe to run at ~10 Hz.
- **Hysteresis is intrinsic.** `idle_s` is *seconds since last input*, so a
  return-then-brief-pause can never reach `idle_threshold_s`; work resumes only
  after continuous idleness.
- **Escalation is idempotent** within one ACTIVE spell — replicas are killed at
  most once per return.
- **DRAINING is terminal** — later polls are no-ops.
- **The poll loop never dies.** Every per-iteration exception is caught and
  logged; overruns are logged and the loop continues.

## Testing

`tests/preempt/` uses in-memory fakes only (`preempt_fakes.py`): a recording
supervisor/sink for call-order assertions, `FakeClock` for deterministic yield
timing, and raising/constant detectors for the loop. No network, no
llama-server, no GPU. The two threaded loop tests exercise timing directly and
run in well under 0.5 s.
