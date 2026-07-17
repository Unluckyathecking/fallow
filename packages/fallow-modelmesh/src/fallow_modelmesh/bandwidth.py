"""Pace chunk transfer so it yields the uplink while the local user is working.

The point of spreading a model over the LAN is to keep it off the uplink, but
even LAN transfer competes with whatever the person at the machine is doing. So
transfer runs fast when the machine is idle and backs off to a trickle when the
user is active, and the switch between the two is driven by state this module is
handed, never state it reads.

modelmesh is a leaf: it does not know how idle is decided and must not import
the agent that decides it. The active/idle signal, the clock, and the sleep are
all injected. That keeps the pacer pure and testable (a fake clock and a
recording sleep make every wait deterministic) and keeps the import-linter leaf
contract intact.

The pacer is a token bucket. It starts full and holds an allowance of bytes that
refills at the rate in force, capped at one second's worth so an idle gap cannot
bank an unbounded burst. A transfer larger than the current allowance waits just
long enough for the shortfall to refill. Selecting the rate per call means a
machine that goes active mid-download slows on the very next chunk.
"""

from collections.abc import Callable


class BandwidthLimiter:
    """Throttle byte transfer to a low rate when active and a high rate when idle.

    ``is_active`` returns whether the local user is currently active, ``clock``
    returns a monotonic time in seconds, and ``sleep`` blocks for a number of
    seconds. All three are injected so the caller owns the transport and this
    stays a pure pacer.
    """

    def __init__(
        self,
        active_rate_bps: float,
        idle_rate_bps: float,
        is_active: Callable[[], bool],
        clock: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> None:
        if active_rate_bps <= 0 or idle_rate_bps <= 0:
            raise ValueError("rates must be positive bytes per second")
        self._active_rate = active_rate_bps
        self._idle_rate = idle_rate_bps
        self._is_active = is_active
        self._clock = clock
        self._sleep = sleep
        # Start full; the per-call cap trims this to one second of the rate in
        # force, so the first transfer flows up to a burst before throttling.
        self._allowance = max(active_rate_bps, idle_rate_bps)
        self._last = clock()

    def _rate_now(self) -> float:
        """Return the rate in force for this call, low when the user is active."""
        return self._active_rate if self._is_active() else self._idle_rate

    def throttle(self, n_bytes: int) -> float:
        """Account for ``n_bytes`` about to transfer, sleeping to hold the rate.

        Refills the allowance for the time elapsed since the last call, then, if
        the transfer exceeds what is available, sleeps for the shortfall at the
        current rate. Returns the seconds slept, which is zero while the
        allowance covers the transfer.
        """
        rate = self._rate_now()
        now = self._clock()
        self._allowance = min(rate, self._allowance + (now - self._last) * rate)
        self._last = now
        if self._allowance >= n_bytes:
            self._allowance -= n_bytes
            return 0.0
        waited = (n_bytes - self._allowance) / rate
        self._sleep(waited)
        self._last = self._clock()
        self._allowance = 0.0
        return waited
