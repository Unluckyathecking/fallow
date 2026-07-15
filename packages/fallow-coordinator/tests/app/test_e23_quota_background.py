"""Quota snapshots run on their own fixed background cadence."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast

from fallow_coordinator.app.background import quota_snapshot_loop
from fallow_coordinator.app.state import CoordinatorState


class Quotas:
    def __init__(self) -> None:
        self.calls = 0

    async def snapshot(self) -> None:
        self.calls += 1


async def test_quota_snapshot_loop_uses_configured_interval() -> None:
    stop = asyncio.Event()
    sleeps: list[float] = []
    quotas = Quotas()

    async def sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) == 2:
            stop.set()

    state = cast(
        CoordinatorState,
        SimpleNamespace(
            stop_event=stop,
            sleep=sleep,
            quotas=quotas,
            config=SimpleNamespace(quota_snapshot_interval_s=17.0),
        ),
    )
    await quota_snapshot_loop(state)

    assert sleeps == [17.0, 17.0]
    assert quotas.calls == 1
