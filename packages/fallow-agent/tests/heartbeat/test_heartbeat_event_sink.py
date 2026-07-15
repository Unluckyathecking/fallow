"""HttpEventSink tests: ordering, non-blocking foreign-thread emit, drop-after-N."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from conftest import AGENT_ID, BASE_URL, DEVICE_TOKEN, instant_sleep

from fallow_agent.heartbeat import CoordinatorClient, EventSinkConfig, HttpEventSink
from fallow_protocol.messages import AgentEvent, EventKind


def _event(seq: int) -> AgentEvent:
    return AgentEvent(
        agent_id=AGENT_ID,
        kind=EventKind.USER_IDLE,
        at=datetime(2026, 7, 15, tzinfo=UTC),
        detail={"seq": str(seq)},
    )


def _client(handler) -> CoordinatorClient:  # type: ignore[no-untyped-def]
    return CoordinatorClient(
        base_url=BASE_URL,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        agent_id=AGENT_ID,
        device_token=DEVICE_TOKEN,
        sleep=instant_sleep,
    )


def _read_jsonl_seqs(path: Path) -> list[str]:
    import json

    return [json.loads(line)["detail"]["seq"] for line in path.read_text().splitlines()]


async def test_events_pushed_and_appended_in_order(tmp_path: Path) -> None:
    received: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request.read().decode())
        return httpx.Response(202)

    path = tmp_path / "events.jsonl"
    sink = HttpEventSink(client=_client(handler), jsonl_path=path, sleep=instant_sleep)
    sink.start()
    for i in range(5):
        sink.emit(_event(i))
    await sink.stop()

    assert _read_jsonl_seqs(path) == ["0", "1", "2", "3", "4"]
    assert len(received) == 5  # all pushed to coordinator
    assert all(f'"seq":"{i}"' in received[i] for i in range(5))


async def test_emit_before_start_is_drained_on_start(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(202)

    path = tmp_path / "events.jsonl"
    sink = HttpEventSink(client=_client(handler), jsonl_path=path, sleep=instant_sleep)

    # Emit while no loop/sender exists yet: must not raise, must be buffered.
    sink.emit(_event(0))
    sink.emit(_event(1))
    sink.start()
    await sink.stop()

    assert _read_jsonl_seqs(path) == ["0", "1"]


async def test_emit_is_nonblocking_from_a_foreign_thread(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(202)

    path = tmp_path / "events.jsonl"
    sink = HttpEventSink(client=_client(handler), jsonl_path=path, sleep=instant_sleep)
    sink.start()

    durations: list[float] = []

    def worker() -> None:
        for i in range(50):
            start = time.perf_counter()
            sink.emit(_event(i))  # called from a plain (non-loop) thread
            durations.append(time.perf_counter() - start)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    await sink.stop()

    assert len(durations) == 50
    assert max(durations) < 0.05  # emit never blocks on the network
    assert _read_jsonl_seqs(path) == [str(i) for i in range(50)]


async def test_push_dropped_after_n_attempts_but_jsonl_kept(tmp_path: Path) -> None:
    attempts = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(500)  # always fails -> transient, retried

    path = tmp_path / "events.jsonl"
    sink = HttpEventSink(
        client=_client(handler),
        jsonl_path=path,
        config=EventSinkConfig(max_push_attempts=3),
        sleep=instant_sleep,
    )
    sink.start()
    sink.emit(_event(0))
    await sink.stop()

    assert attempts["n"] == 3  # exactly N push attempts, then dropped
    assert _read_jsonl_seqs(path) == ["0"]  # durable local copy survives


async def test_auth_failure_drops_immediately_without_retry(tmp_path: Path) -> None:
    attempts = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(401)

    path = tmp_path / "events.jsonl"
    sink = HttpEventSink(
        client=_client(handler),
        jsonl_path=path,
        config=EventSinkConfig(max_push_attempts=3),
        sleep=instant_sleep,
    )
    sink.start()
    sink.emit(_event(0))
    await sink.stop()

    assert attempts["n"] == 1  # auth is not retried
    assert _read_jsonl_seqs(path) == ["0"]
