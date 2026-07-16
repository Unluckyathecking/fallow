"""Unit tests for PollLoop — the dedicated preemption poll thread."""

from __future__ import annotations

import time

from preempt_fakes import ConstantDetector, FakeClock, RaisingDetector, RecordingController

from fallow_agent.preempt import PollLoop
from fallow_protocol.messages import AgentConfig

_STOP_TIMEOUT_S = 0.4


def _wait_until(predicate: object, timeout: float = 0.4) -> bool:
    deadline = time.monotonic() + timeout
    check = predicate  # callable
    while time.monotonic() < deadline:
        if check():  # type: ignore[operator]
            return True
        time.sleep(0.001)
    return False


def test_poll_loop_survives_raising_detector() -> None:
    detector = RaisingDetector()
    controller = RecordingController()
    loop = PollLoop(detector, controller, AgentConfig(poll_interval_ms=2))

    loop.start()
    try:
        assert _wait_until(lambda: detector.calls >= 3)
        assert loop.is_alive()  # a crashing detector never kills the loop
    finally:
        loop.stop(timeout=_STOP_TIMEOUT_S)

    assert not loop.is_alive()
    assert controller.polls == []  # detector raised before controller ran


def test_poll_loop_forwards_idle_and_monotonic_to_controller() -> None:
    detector = ConstantDetector(idle_s=3.5)
    controller = RecordingController()
    clock = FakeClock(t=7.0)
    loop = PollLoop(detector, controller, AgentConfig(poll_interval_ms=2), monotonic=clock)

    loop.start()
    try:
        assert _wait_until(lambda: len(controller.polls) >= 1)
    finally:
        loop.stop(timeout=_STOP_TIMEOUT_S)

    assert not loop.is_alive()
    idle_s, monotonic_now = controller.polls[0]
    assert idle_s == 3.5
    assert monotonic_now == 7.0


def test_poll_loop_skips_preemptor_while_reclaimed() -> None:
    detector = ConstantDetector(idle_s=0.0)
    controller = RecordingController()
    loop = PollLoop(
        detector,
        controller,
        AgentConfig(poll_interval_ms=2),
        reclaim=lambda _monotonic_now: True,  # reclaimed: machine is the user's
    )

    loop.start()
    try:
        time.sleep(0.03)  # several poll periods elapse
    finally:
        loop.stop(timeout=_STOP_TIMEOUT_S)

    # While reclaimed the loop never consults the detector or the preemptor.
    assert detector.calls == 0
    assert controller.polls == []


def test_poll_loop_stop_before_start_is_safe() -> None:
    loop = PollLoop(ConstantDetector(1.0), RecordingController(), AgentConfig())

    loop.stop(timeout=_STOP_TIMEOUT_S)  # never started

    assert not loop.is_alive()
