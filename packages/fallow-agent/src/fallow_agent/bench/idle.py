"""``BenchIdleDetector``: inject a synthetic "user returned" without real HID.

The churn injector (module B2) needs to simulate a user touching the machine on
an agent running headless in a benchmark — there is no keyboard to press. This
decorator wraps any real :class:`~fallow_protocol.interfaces.IdleDetector` and,
after :meth:`simulate_input`, reports ``0`` and counts up from an injected
monotonic clock. It keeps deferring to the injected value until the *inner*
detector reports something smaller — i.e. until a genuine input event resets the
OS idle counter below the synthetic one, at which point real input takes
precedence and the injection is cleared.

Thread-safe: the poll thread reads ``seconds_since_input`` while a bench thread
calls ``simulate_input``. Both touch the injection state only under a lock; the
inner detector is read outside the lock (it is itself O(microseconds) and
thread-safe by contract).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from fallow_agent.bench.constants import ZERO_IDLE_S
from fallow_protocol.interfaces import IdleDetector


class BenchIdleDetector(IdleDetector):
    """Wrap an ``IdleDetector`` so a synthetic input can be injected.

    Passes the inner reading straight through until :meth:`simulate_input` is
    called; from then on it reports elapsed time since that call (``0`` at the
    call instant) until the inner detector reports a smaller value.
    """

    def __init__(
        self,
        inner: IdleDetector,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._inner = inner
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._injected_at: float | None = None

    def seconds_since_input(self) -> float:
        inner_s = self._inner.seconds_since_input()
        with self._lock:
            if self._injected_at is None:
                return inner_s
            simulated_s = max(ZERO_IDLE_S, self._monotonic() - self._injected_at)
            # Real input takes precedence: once the OS counter drops below our
            # synthetic one, a genuine event happened more recently — defer to
            # it and forget the injection.
            if inner_s < simulated_s:
                self._injected_at = None
                return inner_s
            return simulated_s

    def simulate_input(self) -> None:
        """Simulate a user touching the machine: report ``0`` from now, rising."""
        with self._lock:
            self._injected_at = self._monotonic()
