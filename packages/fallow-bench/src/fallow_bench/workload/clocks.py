"""Injected time seam for the workload drivers.

Nothing in this package reads the wall clock or a real monotonic timer
directly: every driver receives a :class:`Clocks` and calls through it. Tests
pass fakes (deterministic ``now``/``monotonic`` and a non-blocking ``sleep``);
``__main__`` passes the real callables. This keeps arrival scheduling and every
recorded timestamp replay-deterministic given ``(config, clocks)``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

MonotonicFn = Callable[[], float]
NowFn = Callable[[], datetime]
SleepFn = Callable[[float], Awaitable[None]]


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Clocks:
    """Bundle of injected clocks: ``monotonic`` paces the arrival schedule,
    ``now`` stamps records, ``sleep`` yields between events."""

    monotonic: MonotonicFn = field(default=time.monotonic)
    now: NowFn = field(default=_utc_now)
    sleep: SleepFn = field(default=asyncio.sleep)
