"""User-triggered takedown: the sticky ``reclaimed`` override.

Automatic preemption (``PreemptController``) reacts to *detected* activity. This
module is the *explicit* override a person uses to reclaim their machine: it
reuses the same suspend-first path for instant compute relief, then stops the
replicas to free RAM/VRAM, and stays down until an explicit release — regardless
of idle detection. That stickiness is the whole point: the user's own work is
never disrupted by fallow deciding the machine looks idle again.

Control channel: a single flag file under the agent state dir (see ADR 042).
Its *presence* means reclaimed; its *absence* means released. The daemon's poll
thread checks it each tick via :class:`ReclaimController`; the ``reclaim`` and
``release`` CLI subcommands write and remove it. A local file is the simplest
cross-platform mechanism (Windows has no POSIX signals) and is never reachable
off-host.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final

from fallow_agent.preempt.constants import MS_PER_S
from fallow_protocol.interfaces import ProcessSupervisor

logger = logging.getLogger(__name__)

RECLAIM_FILENAME: Final[str] = "reclaim.flag"

# How the slow stop-replicas step is dispatched off the hot path. Production
# spawns a daemon thread so the poll tick returns immediately after the suspend;
# tests inject a synchronous runner to assert suspend-then-stop ordering.
StopRunner = Callable[[Callable[[], None]], None]


def reclaim_control_path(state_path: Path) -> Path:
    """The reclaim flag file, alongside the agent state file."""
    return state_path.expanduser().parent / RECLAIM_FILENAME


def request_reclaim(state_path: Path) -> Path:
    """Ask the running daemon to reclaim the machine (write the flag file)."""
    path = reclaim_control_path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    return path


def request_release(state_path: Path) -> Path:
    """Ask the running daemon to release the machine (remove the flag file)."""
    path = reclaim_control_path(state_path)
    path.unlink(missing_ok=True)
    return path


def _spawn_stop(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="fallow-reclaim-stop", daemon=True).start()


class ReclaimController:
    """Drives the sticky reclaimed override from the poll thread.

    Each tick the poll loop calls :meth:`on_poll`. On the rising edge (flag file
    appeared) it suspends every replica immediately — the same hot-path call
    automatic preemption uses — then stops them off the hot path to free memory.
    On the falling edge (flag removed) it clears the state; normal idle-based
    serving resumes and the reconcile loop relaunches replicas on its next tick.
    """

    def __init__(
        self,
        supervisor: ProcessSupervisor,
        control_file: Path,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        stop_runner: StopRunner = _spawn_stop,
    ) -> None:
        self._supervisor = supervisor
        self._control_file = control_file
        self._monotonic = monotonic
        self._stop_runner = stop_runner
        self._lock = threading.Lock()
        self._reclaimed = False

    def is_reclaimed(self) -> bool:
        """Whether the machine is currently reclaimed (thread-safe read)."""
        with self._lock:
            return self._reclaimed

    def on_poll(self, monotonic_now: float) -> bool:
        """Advance the reclaim state one tick; returns whether now reclaimed.

        Returning ``True`` tells the poll loop to skip automatic preemption: the
        machine belongs to the user and must not resume serving.
        """
        wants_reclaim = self._control_file.exists()
        with self._lock:
            if wants_reclaim and not self._reclaimed:
                self._enter_reclaimed(monotonic_now)
            elif not wants_reclaim and self._reclaimed:
                self._release()
            return self._reclaimed

    # ── Transitions (run under self._lock) ───────────────────────────────────

    def _enter_reclaimed(self, monotonic_now: float) -> None:
        # HOT PATH: suspend first for instant relief, measure, then set state.
        self._supervisor.suspend_all()
        yield_ms = (self._monotonic() - monotonic_now) * MS_PER_S
        self._reclaimed = True
        logger.info("reclaimed: suspended all replicas in %.1f ms; stopping now", yield_ms)
        # Stopping waits on process exit, so it must not run on the poll thread.
        # A reclaim immediately followed by a release can race this background
        # stop into killing a replica the reconcile loop just relaunched. It is
        # benign: the next reconcile tick sees the replica STOPPED and desired,
        # and starts it again. We accept that over holding the poll thread.
        self._stop_runner(self._stop_all_replicas)

    def _release(self) -> None:
        self._reclaimed = False
        logger.info("released: normal idle-based serving restored")

    def _stop_all_replicas(self) -> None:
        for status in self._supervisor.statuses():
            self._supervisor.stop_replica(status.model_id)
