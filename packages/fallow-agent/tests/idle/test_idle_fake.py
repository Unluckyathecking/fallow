"""Unit tests for the thread-safe FakeIdleDetector."""

import threading

import pytest

from fallow_agent.idle.fake import FakeIdleDetector


def test_default_is_zero():
    assert FakeIdleDetector().seconds_since_input() == 0.0


def test_initial_value_is_reported():
    assert FakeIdleDetector(idle_s=30.0).seconds_since_input() == 30.0


def test_set_idle_updates_value():
    detector = FakeIdleDetector()
    detector.set_idle(120.0)
    assert detector.seconds_since_input() == 120.0


def test_advance_accumulates():
    detector = FakeIdleDetector(idle_s=5.0)
    detector.advance(2.5)
    assert detector.seconds_since_input() == 7.5


def test_simulate_input_resets_to_zero():
    detector = FakeIdleDetector(idle_s=200.0)
    detector.simulate_input()
    assert detector.seconds_since_input() == 0.0


def test_negative_initial_rejected():
    with pytest.raises(ValueError):
        FakeIdleDetector(idle_s=-1.0)


def test_negative_set_rejected():
    detector = FakeIdleDetector()
    with pytest.raises(ValueError):
        detector.set_idle(-0.001)
    assert detector.seconds_since_input() == 0.0


def test_advance_below_zero_rejected():
    detector = FakeIdleDetector(idle_s=1.0)
    with pytest.raises(ValueError):
        detector.advance(-5.0)
    assert detector.seconds_since_input() == 1.0


def test_thread_safe_under_contention():
    detector = FakeIdleDetector()
    iterations = 2000

    def worker() -> None:
        for i in range(iterations):
            detector.set_idle(float(i))
            detector.seconds_since_input()
            detector.simulate_input()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # No torn reads / crashes; value remains within the invariant.
    assert detector.seconds_since_input() >= 0.0
