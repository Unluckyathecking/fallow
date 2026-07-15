"""Unit tests for PreemptController — the yield/resume/escalate state machine."""

from __future__ import annotations

import pytest
from preempt_fakes import (
    FakeClock,
    RecordingSink,
    RecordingSupervisor,
    fixed_now,
    stopped_status,
    suspended,
)

from fallow_agent.preempt import PreemptController
from fallow_agent.preempt.constants import YIELD_MS_KEY
from fallow_protocol.messages import AgentConfig, AgentState, EventKind


def _config(**overrides: float | int) -> AgentConfig:
    base: dict[str, float | int] = {
        "idle_threshold_s": 120.0,
        "poll_interval_ms": 100,
        "vram_evict_after_s": 60.0,
    }
    base.update(overrides)
    return AgentConfig(**base)  # type: ignore[arg-type]


def _make(
    statuses: tuple = (),
    clock: FakeClock | None = None,
    config: AgentConfig | None = None,
) -> tuple[PreemptController, list[str], RecordingSupervisor, RecordingSink]:
    log: list[str] = []
    supervisor = RecordingSupervisor(log, statuses)
    sink = RecordingSink(log)
    controller = PreemptController(
        supervisor,
        sink,
        config if config is not None else _config(),
        agent_id="agent-1",
        monotonic=clock if clock is not None else FakeClock(),
        now=fixed_now,
    )
    return controller, log, supervisor, sink


def test_fresh_input_suspends_before_emit() -> None:
    controller, log, _supervisor, sink = _make()

    state = controller.on_poll(idle_s=0.0, monotonic_now=0.0)

    assert state is AgentState.ACTIVE
    assert log.index("suspend_all") < log.index("emit:user_returned")
    assert sink.events[0].kind is EventKind.USER_RETURNED
    assert sink.events[0].agent_id == "agent-1"


def test_yield_ms_measured_from_injected_monotonic() -> None:
    clock = FakeClock(t=0.05)  # monotonic() reads 0.05 after suspend
    controller, _log, _supervisor, sink = _make(clock=clock)

    controller.on_poll(idle_s=0.0, monotonic_now=0.0)

    assert sink.events[0].detail[YIELD_MS_KEY] == "50.000"


def test_stale_idle_on_startup_stays_idle() -> None:
    controller, log, _supervisor, _sink = _make()

    # User away: idle_s large, no previous baseline -> no false trigger.
    assert controller.on_poll(idle_s=5.0, monotonic_now=0.0) is AgentState.IDLE
    assert "suspend_all" not in log


def test_fresh_input_via_idle_drop() -> None:
    controller, _log, _supervisor, _sink = _make()

    controller.on_poll(idle_s=5.0, monotonic_now=0.0)
    controller.on_poll(idle_s=5.1, monotonic_now=0.1)  # still climbing -> IDLE
    assert controller.state is AgentState.IDLE
    state = controller.on_poll(idle_s=2.0, monotonic_now=0.2)  # counter reset

    assert state is AgentState.ACTIVE


def test_hysteresis_brief_pause_does_not_resume() -> None:
    controller, log, supervisor, _sink = _make()

    controller.on_poll(idle_s=0.0, monotonic_now=0.0)  # user returns -> ACTIVE
    # Return then a 1s pause: must NOT resume work.
    assert controller.on_poll(idle_s=1.0, monotonic_now=1.0) is AgentState.ACTIVE
    assert "resume_all" not in log
    assert supervisor.stopped == []

    # Only genuinely-continuous idleness past the threshold resumes.
    state = controller.on_poll(idle_s=120.0, monotonic_now=120.0)
    assert state is AgentState.IDLE
    assert "resume_all" in log
    assert log[-1] == "emit:user_idle"


def test_escalation_kills_only_suspended_gpu_replicas_after_evict_delay() -> None:
    statuses = (
        suspended("a", 8001),
        suspended("c", 8003),
        suspended("cpu-only", 8004, gpu=False),  # suspended CPU replica survives
        stopped_status("b", 8002),
    )
    controller, _log, supervisor, _sink = _make(statuses=statuses)

    controller.on_poll(idle_s=0.0, monotonic_now=0.0)  # ACTIVE at t=0
    controller.on_poll(idle_s=1.0, monotonic_now=59.0)  # below evict delay
    assert supervisor.stopped == []

    controller.on_poll(idle_s=1.0, monotonic_now=61.0)  # past evict delay
    assert supervisor.stopped == ["a", "c"]

    # Idempotent: a later poll does not re-kill.
    controller.on_poll(idle_s=1.0, monotonic_now=62.0)
    assert supervisor.stopped == ["a", "c"]


def test_draining_is_terminal_and_ignores_transitions() -> None:
    controller, log, _supervisor, sink = _make()

    controller.drain()
    assert controller.state is AgentState.DRAINING
    assert sink.events[-1].kind is EventKind.AGENT_STOPPING

    baseline = list(log)
    # Fresh input after drain must not suspend or transition.
    assert controller.on_poll(idle_s=0.0, monotonic_now=0.0) is AgentState.DRAINING
    assert log == baseline
    assert "suspend_all" not in log


def test_drain_is_idempotent() -> None:
    controller, _log, _supervisor, sink = _make()

    controller.drain()
    controller.drain()

    stopping = [e for e in sink.events if e.kind is EventKind.AGENT_STOPPING]
    assert len(stopping) == 1


@pytest.mark.parametrize("idle_s", [0.0, 0.05])
def test_fresh_input_boundary_within_poll_interval(idle_s: float) -> None:
    # poll_interval_s = 0.1; idle below it counts as fresh input.
    controller, _log, _supervisor, _sink = _make()

    assert controller.on_poll(idle_s=idle_s, monotonic_now=0.0) is AgentState.ACTIVE
