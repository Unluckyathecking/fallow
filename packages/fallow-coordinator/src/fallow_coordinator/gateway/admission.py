"""Bounded FIFO waiting room for temporarily unavailable interactive replicas."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeVar

T = TypeVar("T")
Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]
Probe = Callable[[], Awaitable[T | None]]


class AdmissionStatus(StrEnum):
    ADMITTED = "admitted"
    OVERFLOW = "overflow"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class AdmissionResult[T]:
    status: AdmissionStatus
    value: T | None
    waited_ms: int


class AdmissionQueue:
    """Wait for capacity without allowing newer requests past older peers."""

    def __init__(
        self,
        *,
        capacity: int,
        timeout_s: float,
        poll_interval_s: float,
        clock: Clock,
        sleep: Sleeper,
    ) -> None:
        if capacity <= 0:
            raise ValueError("admission capacity must be positive")
        if timeout_s < 0:
            raise ValueError("admission timeout must not be negative")
        if poll_interval_s <= 0:
            raise ValueError("admission poll interval must be positive")
        self._capacity = capacity
        self._timeout_s = timeout_s
        self._poll_interval_s = poll_interval_s
        self._clock = clock
        self._sleep = sleep
        self._lanes: dict[Hashable, deque[object]] = {}
        self._waiting = 0
        self._lock = asyncio.Lock()

    async def wait(self, lane: Hashable, probe: Probe[T]) -> AdmissionResult[T]:
        started = self._clock()
        ticket = object()
        async with self._lock:
            if self._waiting >= self._capacity:
                return AdmissionResult(AdmissionStatus.OVERFLOW, None, 0)
            queue = self._lanes.setdefault(lane, deque())
            queue.append(ticket)
            self._waiting += 1

        try:
            while True:
                if await self._is_head(lane, ticket):
                    value = await probe()
                    if value is not None:
                        await self._remove(lane, ticket)
                        return AdmissionResult(
                            AdmissionStatus.ADMITTED,
                            value,
                            _elapsed_ms(self._clock() - started),
                        )
                elapsed = self._clock() - started
                remaining = self._timeout_s - elapsed
                if remaining <= 0:
                    await self._remove(lane, ticket)
                    return AdmissionResult(
                        AdmissionStatus.TIMEOUT, None, _elapsed_ms(max(elapsed, 0.0))
                    )
                await self._sleep(min(self._poll_interval_s, remaining))
        except BaseException:
            await self._remove(lane, ticket)
            raise

    async def _is_head(self, lane: Hashable, ticket: object) -> bool:
        async with self._lock:
            queue = self._lanes.get(lane)
            if queue is None or not queue:
                return False
            return queue[0] is ticket

    async def _remove(self, lane: Hashable, ticket: object) -> None:
        async with self._lock:
            queue = self._lanes.get(lane)
            if queue is None:
                return
            try:
                queue.remove(ticket)
            except ValueError:
                return
            self._waiting -= 1
            if not queue:
                del self._lanes[lane]


def _elapsed_ms(seconds: float) -> int:
    return round(seconds * 1000)
