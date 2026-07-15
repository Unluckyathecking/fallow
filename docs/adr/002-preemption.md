# ADR 002: Preemption state machine (module A2)

Status: accepted · Date: 2026-07-15

## Context

Fallow's promise (ADR 000, decision 3) is that users never notice it: all
fallow-owned inference must yield within ~300 ms (p99) of user input. The yield
decision is a tiny state machine driven by a high-frequency idle poll. It must
be hot-path safe, deterministic to test, and impossible to wedge.

## Decisions

1. **Synchronous state machine on a dedicated OS thread.** `PollLoop` is a plain
   `threading.Thread`, not asyncio: the safety-critical path must not share a
   scheduler with the agent's event loop, so no coroutine can delay a suspend.
2. **Suspend is the first side effect on return.** In `_enter_active`,
   `supervisor.suspend_all()` runs before the yield-latency read and before
   `emit`. The state lock is uncontended (only `on_poll` and `drain` contend),
   so acquiring it costs nanoseconds and does not violate this.
3. **Escalation without an ABC change.** The `Preemptor` requirement is to free
   VRAM after `vram_evict_after_s`, but `ProcessSupervisor` exposes no
   `escalate()`. Rather than change the contract, the controller reads
   `supervisor.statuses()` and calls `stop_replica(model_id)` for every replica
   still `SUSPENDED`, once per ACTIVE spell (an `_escalated` latch). Restart is
   the reconcile loop's job, not ours.
4. **Hysteresis for free.** `idle_s` is *seconds since input*, so ACTIVE→IDLE on
   `idle_s >= idle_threshold_s` inherently ignores a return followed by a brief
   pause — no separate timer needed.
5. **The poll loop cannot die.** Every iteration's body is wrapped; exceptions
   are logged and polling continues. A dead loop means Fallow never yields —
   the one unacceptable failure — so surviving a crashing detector is a tested
   invariant.
6. **Injected clocks.** `monotonic()` (yield timing / escalation) and `now()`
   (event timestamps) are constructor-injected, making every test deterministic
   with no real sleeps except the two that test loop timing itself.

## Consequences

- The controller needs `agent_id` and a wall-clock `now()` that the task's
  sketched constructor omitted, because `AgentEvent` requires both — added as
  constructor params (see open questions).
- Killing suspended replicas relies on the supervisor's reconcile loop to
  restart them once the user leaves; the controller never restarts.
- `idle_s < prev_idle_s` treats any decrease as fresh input; detector jitter is
  tolerated because the `idle_s < poll_interval_s` guard is the safe path.
