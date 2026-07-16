"""The dedicated poll thread that drives the preemption state machine.

A plain ``threading.Thread`` — deliberately NOT asyncio. The safety-critical
path (detect input → suspend) must not share a scheduler with anything else, so
event-loop jitter can never delay a yield.

Invariant: the loop NEVER dies. A dead poll loop means Fallow stops yielding to
the user, which is the one failure mode we must not have. Every exception inside
an iteration is caught, logged, and the loop continues.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from fallow_agent.preempt.constants import MS_PER_S, POLL_THREAD_NAME
from fallow_protocol.interfaces import IdleDetector, Preemptor
from fallow_protocol.messages import AgentConfig


class PollLoop(threading.Thread):
    """Calls the idle detector then the controller every ``poll_interval_ms``.

    Overruns (an iteration slower than the period) are logged and the next
    iteration starts immediately. Use ``start()`` (inherited) and ``stop()``.
    """

    def __init__(
        self,
        detector: IdleDetector,
        controller: Preemptor,
        config: AgentConfig,
        monotonic: Callable[[], float] = time.monotonic,
        logger: logging.Logger | None = None,
        reclaim: Callable[[float], bool] | None = None,
    ) -> None:
        super().__init__(name=POLL_THREAD_NAME, daemon=True)
        self._detector = detector
        self._controller = controller
        self._period_s = config.poll_interval_ms / MS_PER_S
        self._monotonic = monotonic
        self._log = logger if logger is not None else logging.getLogger(__name__)
        self._reclaim = reclaim
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.is_set():
            start = self._monotonic()
            self._safe_poll(start)
            self._wait_remainder(start)

    def stop(self, timeout: float | None = None) -> None:
        """Signal the loop to exit and (optionally) join the thread."""
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout)

    # ── Internals ───────────────────────────────────────────────────────────

    def _safe_poll(self, monotonic_now: float) -> None:
        try:
            # Reclaim is a sticky override: while it holds, the machine is the
            # user's and automatic preemption is skipped so nothing resumes.
            if self._reclaim is not None and self._reclaim(monotonic_now):
                return
            idle_s = self._detector.seconds_since_input()
            self._controller.on_poll(idle_s, monotonic_now)
        except Exception:
            # A crashing detector/controller must never kill the loop.
            self._log.exception("preempt poll iteration failed; continuing")

    def _wait_remainder(self, start: float) -> None:
        remaining = self._period_s - (self._monotonic() - start)
        if remaining > 0:
            self._stop_event.wait(remaining)
        else:
            self._log.warning("preempt poll overrun by %.1f ms", -remaining * MS_PER_S)
