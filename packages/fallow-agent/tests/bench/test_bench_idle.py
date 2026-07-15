"""BenchIdleDetector: passthrough, injection, and real-input precedence.

All deterministic — a settable ``FakeIdleDetector`` for the inner reading and a
settable fake monotonic clock. No OS, no real time.
"""

from __future__ import annotations

from fallow_agent.bench import BenchIdleDetector
from fallow_agent.idle import FakeIdleDetector


class FakeClock:
    """A settable monotonic source; ``t`` is what the next call returns."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_passthrough_before_injection() -> None:
    inner = FakeIdleDetector(idle_s=42.0)
    bench = BenchIdleDetector(inner, monotonic=FakeClock(100.0))

    assert bench.seconds_since_input() == 42.0

    inner.set_idle(7.5)
    assert bench.seconds_since_input() == 7.5  # tracks the inner detector


def test_injection_reports_zero_then_counts_up() -> None:
    inner = FakeIdleDetector(idle_s=300.0)  # machine looks idle
    clock = FakeClock(1000.0)
    bench = BenchIdleDetector(inner, monotonic=clock)

    bench.simulate_input()
    assert bench.seconds_since_input() == 0.0  # 0 at the instant of injection

    clock.t = 1002.5
    assert bench.seconds_since_input() == 2.5  # rises with the monotonic clock

    clock.t = 1030.0
    assert bench.seconds_since_input() == 30.0


def test_real_input_takes_precedence_over_injection() -> None:
    inner = FakeIdleDetector(idle_s=300.0)
    clock = FakeClock(0.0)
    bench = BenchIdleDetector(inner, monotonic=clock)

    bench.simulate_input()
    clock.t = 5.0
    assert bench.seconds_since_input() == 5.0  # injection still in effect (5 < 300)

    # A genuine input event resets the OS counter below the synthetic value.
    inner.set_idle(0.2)
    assert bench.seconds_since_input() == 0.2  # real input wins

    # And the injection is cleared: further inner readings pass straight through
    # even though the synthetic clock kept advancing.
    clock.t = 500.0
    inner.set_idle(9.0)
    assert bench.seconds_since_input() == 9.0


def test_injection_clears_when_synthetic_overtakes_a_constant_inner() -> None:
    inner = FakeIdleDetector(idle_s=3.0)
    clock = FakeClock(0.0)
    bench = BenchIdleDetector(inner, monotonic=clock)

    bench.simulate_input()
    clock.t = 1.0
    assert bench.seconds_since_input() == 1.0  # synthetic below inner

    # Once the synthetic value passes the (constant) inner reading, the inner
    # value is the smaller one and takes over permanently.
    clock.t = 10.0
    assert bench.seconds_since_input() == 3.0

    clock.t = 20.0
    assert bench.seconds_since_input() == 3.0  # injection stayed cleared


def test_reinjection_after_clear() -> None:
    inner = FakeIdleDetector(idle_s=100.0)
    clock = FakeClock(0.0)
    bench = BenchIdleDetector(inner, monotonic=clock)

    bench.simulate_input()
    inner.set_idle(0.0)
    assert bench.seconds_since_input() == 0.0  # real input cleared injection

    clock.t = 50.0
    inner.set_idle(100.0)
    bench.simulate_input()  # inject again
    clock.t = 53.0
    assert bench.seconds_since_input() == 3.0
