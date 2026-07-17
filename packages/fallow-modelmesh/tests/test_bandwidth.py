from fallow_modelmesh.bandwidth import BandwidthLimiter


class FakeClock:
    """A clock the test advances by hand, with a sleep that moves it forward."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _limiter(clock: FakeClock, *, active: bool) -> BandwidthLimiter:
    return BandwidthLimiter(
        active_rate_bps=100.0,
        idle_rate_bps=10_000.0,
        is_active=lambda: active,
        clock=clock.time,
        sleep=clock.sleep,
    )


def test_rejects_non_positive_rates() -> None:
    clock = FakeClock()
    for active, idle in ((0.0, 100.0), (100.0, -1.0)):
        try:
            BandwidthLimiter(active, idle, lambda: False, clock.time, clock.sleep)
        except ValueError:
            continue
        raise AssertionError("expected ValueError for non-positive rate")


def test_active_transfer_beyond_the_burst_waits_for_the_low_rate() -> None:
    clock = FakeClock()
    limiter = _limiter(clock, active=True)
    # Bucket starts with one second at 100 B/s. 500 bytes needs 400 more at
    # 100 B/s, so it sleeps four seconds.
    assert limiter.throttle(500) == 4.0
    assert clock.slept == [4.0]


def test_idle_transfer_of_the_same_size_does_not_wait() -> None:
    clock = FakeClock()
    limiter = _limiter(clock, active=False)
    # The idle rate covers 500 bytes inside the first second's burst.
    assert limiter.throttle(500) == 0.0
    assert clock.slept == []


def test_refill_over_elapsed_time_lets_a_later_transfer_through() -> None:
    clock = FakeClock()
    limiter = _limiter(clock, active=True)
    limiter.throttle(100)  # drains the starting burst
    clock.now += 1.0  # one second refills 100 bytes at 100 B/s
    assert limiter.throttle(80) == 0.0


def test_burst_is_capped_at_one_second_of_rate() -> None:
    clock = FakeClock()
    limiter = _limiter(clock, active=True)
    limiter.throttle(100)  # drains the starting burst
    clock.now += 100.0  # a long idle gap must not bank 100 seconds of tokens
    # Only one second (100 bytes) is available, so 300 bytes waits for 200 more.
    assert limiter.throttle(300) == 2.0


def test_rate_follows_state_between_calls() -> None:
    clock = FakeClock()
    active = {"value": False}
    limiter = BandwidthLimiter(
        active_rate_bps=100.0,
        idle_rate_bps=10_000.0,
        is_active=lambda: active["value"],
        clock=clock.time,
        sleep=clock.sleep,
    )
    assert limiter.throttle(5_000) == 0.0  # idle: covered by the burst
    active["value"] = True
    assert limiter.throttle(500) > 0.0  # now active: throttled


def test_is_deterministic_across_runs() -> None:
    def run() -> list[float]:
        clock = FakeClock()
        limiter = _limiter(clock, active=True)
        return [limiter.throttle(n) for n in (100, 250, 400)]

    assert run() == run()
