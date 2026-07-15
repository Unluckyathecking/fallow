"""Deterministic fakes for the workload tests (no real network, no real clock).

Provides an incrementing datetime clock, a constant monotonic clock, a
non-blocking sleep, an in-memory recording writer, and SSE MockTransport
builders that role-play the gateway.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta

import httpx

from fallow_bench.workload.clocks import Clocks


class StepClock:
    """Returns a UTC datetime one second later on each call (deterministic)."""

    def __init__(self) -> None:
        self._base = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)
        self._n = itertools.count()

    def __call__(self) -> datetime:
        return self._base + timedelta(seconds=next(self._n))


class RecordingSleep:
    """Async sleep that records requested durations and never really blocks."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        await asyncio.sleep(0)  # yield so other tasks progress


def make_clocks(sleep: Callable[[float], Awaitable[None]] | None = None) -> Clocks:
    """Fake clocks: incrementing ``now``, constant ``monotonic``, fake ``sleep``."""
    return Clocks(
        monotonic=lambda: 0.0,
        now=StepClock(),
        sleep=sleep or RecordingSleep(),
    )


class RecordingWriter:
    """Duck-typed :class:`JsonlWriter` that keeps records in memory."""

    def __init__(self) -> None:
        self.records: list[object] = []

    def write(self, record: object) -> None:
        self.records.append(record)

    def close(self) -> None:  # pragma: no cover - parity with JsonlWriter
        pass


def sse_bytes(n_chunks: int) -> list[bytes]:
    """A well-formed SSE body: ``n_chunks`` data frames then ``[DONE]``."""
    frames = [f'data: {{"i":{i}}}\n\n'.encode() for i in range(n_chunks)]
    frames.append(b"data: [DONE]\n\n")
    return frames


def sse_transport(chunks: Sequence[bytes]) -> httpx.MockTransport:
    """MockTransport that streams ``chunks`` as one 200 SSE response."""

    async def handler(_request: httpx.Request) -> httpx.Response:
        async def gen():
            for chunk in chunks:
                yield chunk

        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=gen())

    return httpx.MockTransport(handler)


def async_client(transport: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=transport, base_url="http://coord.test")
