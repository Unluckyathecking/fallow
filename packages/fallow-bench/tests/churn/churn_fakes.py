"""Deterministic fakes for the churn-injector tests.

No real network, no real clock, no subprocess. A fake monotonic clock advanced
by a fake sleeper, an httpx.MockTransport scripted per path, a recording runner,
and a list-appending record sink — so tests assert on ordering and values
exactly.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx

from fallow_bench.churn.models import ChurnRecord, RunResult


class FakeClock:
    """A monotonic source advanced only by FakeSleeper. ``t`` is next reading."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeSleeper:
    """Advances a FakeClock instead of really sleeping; records each delay."""

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        self._clock.t += delay


class RecordingSink:
    """Collects every ChurnRecord the injector writes, in order."""

    def __init__(self) -> None:
        self.records: list[ChurnRecord] = []

    async def __call__(self, record: ChurnRecord) -> None:
        self.records.append(record)


class RecordingRunner:
    """Records rendered commands and returns a canned RunResult."""

    def __init__(self, result: RunResult | None = None) -> None:
        self.commands: list[str] = []
        self._result = result if result is not None else RunResult(ok=True, detail="done")

    async def __call__(self, command: str) -> RunResult:
        self.commands.append(command)
        return self._result


async def raising_runner(command: str) -> RunResult:
    """A broken runner: raises to prove the injector survives it."""
    raise RuntimeError(f"boom: {command}")


StateSeq = Callable[[], str]


def mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    """An AsyncClient whose every request is served by ``handler`` in-memory."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def input_ok_handler() -> Callable[[httpx.Request], httpx.Response]:
    """POST /simulate_input → 204; GET /state → active immediately."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/simulate_input"):
            return httpx.Response(204)
        return httpx.Response(200, json={"state": "active", "idle_s": 0.0})

    return handler
