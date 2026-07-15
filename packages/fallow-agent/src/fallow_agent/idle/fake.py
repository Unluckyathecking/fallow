"""In-memory idle detector for unit tests and the bench churn injector.

Thread-safe: the poll thread reads while a test/bench thread mutates the value.
The stored idle value is guarded by a lock so reads and writes never tear.
"""

import threading

from fallow_agent.idle.constants import NEGATIVE_IDLE_MSG, ZERO_IDLE_S
from fallow_protocol.interfaces import IdleDetector


class FakeIdleDetector(IdleDetector):
    """A settable, thread-safe `IdleDetector`.

    Invariant: the reported idle value is always >= 0.
    """

    def __init__(self, idle_s: float = ZERO_IDLE_S) -> None:
        self._lock = threading.Lock()
        self._idle_s = self._validated(idle_s)

    @staticmethod
    def _validated(idle_s: float) -> float:
        if idle_s < ZERO_IDLE_S:
            raise ValueError(NEGATIVE_IDLE_MSG)
        return idle_s

    def seconds_since_input(self) -> float:
        with self._lock:
            return self._idle_s

    def set_idle(self, idle_s: float) -> None:
        """Set the reported idle value (must be >= 0)."""
        validated = self._validated(idle_s)
        with self._lock:
            self._idle_s = validated

    def advance(self, delta_s: float) -> None:
        """Increase the idle value by ``delta_s`` (simulates time passing)."""
        with self._lock:
            self._idle_s = self._validated(self._idle_s + delta_s)

    def simulate_input(self) -> None:
        """Simulate a user touching the machine: reset idle to zero."""
        with self._lock:
            self._idle_s = ZERO_IDLE_S
