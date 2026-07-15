# preempt

Fallow's yield/resume/escalate state machine — the agent's signature feature.
`Controller` is a 1:1 port of `fallow_agent.preempt.controller.PreemptController`
and implements the `Preemptor` contract. It is driven one tick at a time by an
external poll loop (the caller reads the idle detector, then calls `OnPoll`).

## The one hard rule

When the user returns, the **first** observable side effect is
`ProcessSupervisor.SuspendAll()`. Nothing may run before it. `enterActive`
suspends first, measures the yield latency second, and emits the event third —
in that order, deliberately.

## State transitions

States are `protocol.AgentState`:

| From | Trigger | To | Side effects |
| --- | --- | --- | --- |
| Idle | fresh user input | Active | `SuspendAll`, emit `user_returned` (with `yield_ms`) |
| Active | held ≥ `vram_evict_after_s` | Active | stop suspended **GPU** replicas, once |
| Active | idle ≥ `idle_threshold_s` | Idle | `ResumeAll`, emit `user_idle` |
| any | `Drain()` | Draining | emit `agent_stopping` (terminal) |

## Fresh input and intrinsic hysteresis

`idle_s` is *seconds since last input*. Input is "fresh" when `idle_s` is below
one poll interval, or when it dropped below the previous sample (the OS counter
reset). Because a brief pause after a return still has a small-but-growing
`idle_s`, it can never reach `idle_threshold_s` — so work only resumes after
genuinely continuous idleness. The hysteresis is a property of the signal, not
an extra timer.

## GPU-only escalation

If the user stays for `vram_evict_after_s`, the controller stops **only**
suspended GPU replicas, exactly once (`escalated` latch). A suspended CPU
replica costs the user nothing they notice; pinned VRAM breaks whatever they
just launched.

## Draining is terminal

`Drain()` is idempotent and irreversible: once Draining, `OnPoll` is a no-op and
no further events fire.

## Thread-safety

`OnPoll` (poll thread) and `Drain` (shutdown thread) mutate under one uncontended
mutex guarding only in-memory bookkeeping and non-blocking supervisor/sink calls,
so the lock never delays the actual suspend. Clocks (`Monotonic`, `Now`) are
injectable; `controller_test.go` ports the Python `test_preempt_controller.py`
cases case-for-case with deterministic fakes.
