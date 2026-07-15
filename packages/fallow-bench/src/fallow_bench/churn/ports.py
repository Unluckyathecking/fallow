"""Injected-collaborator seams for the churn injector.

The injector owns no clock, no sleeper, no subprocess, and no file handle
directly. Every side effect enters through one of these callables so unit tests
replay a fake clock, a fake HTTP transport, and a recording runner — and the
run is byte-for-byte deterministic.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fallow_bench.churn.models import ChurnRecord, RunResult

# Monotonic time source (seconds). Real wiring injects ``time.monotonic``.
Clock = Callable[[], float]

# Async sleep. Real wiring injects ``asyncio.sleep``; tests advance a fake clock.
Sleeper = Callable[[float], Awaitable[None]]

# Executes one rendered shell command (kill / net-drop). Real wiring injects the
# subprocess runner; tests inject a recorder. Must never raise.
Runner = Callable[[str], Awaitable[RunResult]]

# Where executed events are recorded. ``ChurnLog.write`` satisfies this; tests
# inject a list-appending fake.
RecordSink = Callable[[ChurnRecord], Awaitable[None]]
