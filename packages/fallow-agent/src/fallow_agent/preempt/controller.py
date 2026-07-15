"""The yield/resume/escalate state machine — Fallow's signature feature.

`PreemptController` implements ``fallow_protocol.interfaces.Preemptor``. It is
driven one tick at a time by the poll thread (see ``poll_loop``). The single
hard rule of this module: when the user returns, the *first* observable side
effect is ``supervisor.suspend_all()`` — nothing may run before it.

State transitions (states are ``AgentState``):

    IDLE  --fresh user input-->        ACTIVE   (suspend_all, emit USER_RETURNED)
    ACTIVE --held vram_evict_after_s--> ACTIVE   (stop suspended replicas once)
    ACTIVE --idle >= idle_threshold_s-> IDLE     (resume_all, emit USER_IDLE)
    any   --drain()-->                 DRAINING  (terminal; emit AGENT_STOPPING)

Hysteresis is intrinsic: ``idle_s`` is *seconds since last input*, so a brief
pause after a return can never reach ``idle_threshold_s`` — work only resumes
after genuinely continuous idleness.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from fallow_agent.preempt.constants import MS_PER_S, YIELD_MS_KEY
from fallow_protocol.interfaces import EventSink, Preemptor, ProcessSupervisor
from fallow_protocol.messages import AgentConfig, AgentEvent, AgentState, EventKind
from fallow_protocol.models import ReplicaState


def _utc_now() -> datetime:
    """Default wall-clock source for event timestamps (injectable for tests)."""
    return datetime.now(UTC)


class PreemptController(Preemptor):
    """Hot-path preemption decision engine.

    Thread-safety: ``on_poll`` (poll thread) and ``drain`` (shutdown thread)
    mutate state under an uncontended lock. The lock guards only in-memory
    bookkeeping and non-blocking supervisor/sink calls, so acquiring it costs
    nanoseconds and never delays the actual suspend.
    """

    def __init__(
        self,
        supervisor: ProcessSupervisor,
        sink: EventSink,
        config: AgentConfig,
        agent_id: str,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._supervisor = supervisor
        self._sink = sink
        self._config = config
        self._agent_id = agent_id
        self._monotonic = monotonic
        self._now = now
        self._poll_interval_s = config.poll_interval_ms / MS_PER_S

        self._lock = threading.Lock()
        self._state: AgentState = AgentState.IDLE
        self._prev_idle_s: float | None = None
        self._active_since: float | None = None
        self._escalated = False

    # ── Preemptor contract ──────────────────────────────────────────────────

    @property
    def state(self) -> AgentState:
        with self._lock:
            return self._state

    def on_poll(self, idle_s: float, monotonic_now: float) -> AgentState:
        with self._lock:
            if self._state is AgentState.DRAINING:
                return self._state
            if self._state is AgentState.IDLE:
                if self._is_fresh_input(idle_s):
                    self._enter_active(monotonic_now)
            elif self._state is AgentState.ACTIVE:
                if idle_s >= self._config.idle_threshold_s:
                    self._enter_idle()
                else:
                    self._maybe_escalate(monotonic_now)
            self._prev_idle_s = idle_s
            return self._state

    # ── Public shutdown ─────────────────────────────────────────────────────

    def drain(self) -> None:
        """Enter the terminal DRAINING state. Idempotent; accepts no new work."""
        with self._lock:
            if self._state is AgentState.DRAINING:
                return
            self._state = AgentState.DRAINING
            self._emit(EventKind.AGENT_STOPPING, {})

    # ── Transitions (all run under self._lock) ──────────────────────────────

    def _is_fresh_input(self, idle_s: float) -> bool:
        if idle_s < self._poll_interval_s:
            return True
        return self._prev_idle_s is not None and idle_s < self._prev_idle_s

    def _enter_active(self, monotonic_now: float) -> None:
        # HOT PATH: suspend first, measure second, emit third. Do not reorder.
        self._supervisor.suspend_all()
        yield_ms = (self._monotonic() - monotonic_now) * MS_PER_S
        self._state = AgentState.ACTIVE
        self._active_since = monotonic_now
        self._escalated = False
        self._emit(EventKind.USER_RETURNED, {YIELD_MS_KEY: f"{yield_ms:.3f}"})

    def _enter_idle(self) -> None:
        self._supervisor.resume_all()
        self._state = AgentState.IDLE
        self._active_since = None
        self._escalated = False
        self._emit(EventKind.USER_IDLE, {})

    def _maybe_escalate(self, monotonic_now: float) -> None:
        if self._escalated or self._active_since is None:
            return
        if monotonic_now - self._active_since < self._config.vram_evict_after_s:
            return
        # Only GPU replicas: a suspended CPU replica costs nothing the user
        # notices, but pinned VRAM breaks whatever the returning user launches.
        for status in self._supervisor.statuses():
            if status.state is ReplicaState.SUSPENDED and status.gpu:
                self._supervisor.stop_replica(status.model_id)
        self._escalated = True

    def _emit(self, kind: EventKind, detail: Mapping[str, str]) -> None:
        event = AgentEvent(
            agent_id=self._agent_id,
            kind=kind,
            at=self._now(),
            detail=dict(detail),
        )
        self._sink.emit(event)
