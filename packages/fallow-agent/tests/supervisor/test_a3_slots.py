"""llama-server slot parsing and supervisor occupancy publication."""

from __future__ import annotations

import http.client
import logging
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from fallow_agent.supervisor import (
    ChildProcessSupervisor,
    SupervisorConfig,
    http_busy_slot_count,
    parse_busy_slots,
)
from fallow_protocol.models import ModelManifest, ReplicaState, WorkerKind

_DEADLINE_S = 0.5
_POLL_S = 0.01


def _manifest() -> ModelManifest:
    return ModelManifest(
        model_id="tiny",
        family="tiny",
        quant="Q4_K_M",
        worker_kind=WorkerKind.CHAT,
        file_name="tiny.gguf",
        sha256="a" * 64,
        size_bytes=1,
    )


def _sleeper(_manifest: ModelManifest, _path: Path, _port: int) -> list[str]:
    return [sys.executable, "-c", "import time; time.sleep(60)"]


def _wait_for(predicate: Callable[[], bool]) -> bool:
    deadline = time.monotonic() + _DEADLINE_S
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(_POLL_S)
    return predicate()


class _Healthy:
    def __call__(self, host: str, port: int, path: str, timeout_s: float) -> bool:
        return True


class _MutableSlots:
    def __init__(self, value: int | None = 0) -> None:
        self.value = value
        self.error = False
        self.calls = 0

    def __call__(self, host: str, port: int, timeout_s: float) -> int | None:
        self.calls += 1
        if self.error:
            raise RuntimeError("fake slot endpoint failed")
        return self.value


class _Response:
    def __init__(self, status: int, payload: bytes) -> None:
        self.status = status
        self._payload = payload

    def read(self) -> bytes:
        return self._payload


class _Connection:
    response = _Response(200, b"[]")
    requested: tuple[str, str] | None = None

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout

    def request(self, method: str, path: str) -> None:
        type(self).requested = (method, path)

    def getresponse(self) -> _Response:
        return type(self).response

    def close(self) -> None:
        pass


def test_parse_busy_slots_counts_processing_entries() -> None:
    payload = (
        b'[{"id":0,"is_processing":true},'
        b'{"id":1,"is_processing":false},'
        b'{"id":2,"is_processing":true}]'
    )
    assert parse_busy_slots(payload) == 2


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b"{}",
        b'[{"id":0}]',
        b'[{"is_processing":1}]',
        b"[true]",
    ],
)
def test_parse_busy_slots_rejects_unknown_shapes(payload: bytes) -> None:
    assert parse_busy_slots(payload) is None


def test_http_slot_probe_uses_pinned_slots_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    _Connection.response = _Response(
        200,
        b'[{"is_processing":false},{"is_processing":true}]',
    )
    _Connection.requested = None
    monkeypatch.setattr(http.client, "HTTPConnection", _Connection)

    assert http_busy_slot_count("127.0.0.1", 8080, 0.2) == 1
    assert _Connection.requested == ("GET", "/slots")


def test_http_slot_probe_tolerates_missing_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    _Connection.response = _Response(501, b'{"error":"disabled"}')
    monkeypatch.setattr(http.client, "HTTPConnection", _Connection)
    assert http_busy_slot_count("127.0.0.1", 8080, 0.2) is None


def test_health_thread_publishes_slots_and_survives_probe_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="fallow_agent.supervisor.supervisor")
    slots = _MutableSlots(2)
    supervisor = ChildProcessSupervisor(
        SupervisorConfig(
            llama_binary=Path("/usr/bin/true"),
            startup_timeout_s=_DEADLINE_S,
            health_poll_interval_s=_POLL_S,
            stop_grace_s=_DEADLINE_S,
        ),
        _sleeper,
        health_check=_Healthy(),
        slots_check=slots,
    )
    try:
        supervisor.start_replica(_manifest(), Path("/models/tiny.gguf"), 8080)
        assert _wait_for(
            lambda: (
                supervisor.statuses()[0].state is ReplicaState.READY
                and supervisor.statuses()[0].inflight == 2
            )
        )
        calls_before_failure = slots.calls
        slots.error = True
        assert _wait_for(lambda: slots.calls > calls_before_failure + 1)
        assert supervisor.statuses()[0].inflight == 2
        assert supervisor._threads["tiny"].is_alive()
        failures = [
            record
            for record in caplog.records
            if "slot occupancy unavailable" in record.getMessage()
        ]
        assert len(failures) == 1
        child = supervisor._children["tiny"]
        slots.error = False
        slots.value = 3
        supervisor.stop_replica("tiny")
        supervisor._poll_slots(child)
        assert supervisor.statuses()[0].state is ReplicaState.STOPPED
        assert supervisor.statuses()[0].inflight == 0
    finally:
        supervisor.stop_all()


def test_missing_slot_endpoint_starts_at_zero() -> None:
    slots = _MutableSlots(None)
    supervisor = ChildProcessSupervisor(
        SupervisorConfig(
            llama_binary=Path("/usr/bin/true"),
            startup_timeout_s=_DEADLINE_S,
            health_poll_interval_s=_POLL_S,
            stop_grace_s=_DEADLINE_S,
        ),
        _sleeper,
        health_check=_Healthy(),
        slots_check=slots,
    )
    try:
        supervisor.start_replica(_manifest(), Path("/models/tiny.gguf"), 8080)
        assert _wait_for(lambda: supervisor.statuses()[0].state is ReplicaState.READY)
        assert supervisor.statuses()[0].inflight == 0
    finally:
        supervisor.stop_all()


def test_late_failed_probe_does_not_mark_replacement_warned() -> None:
    slots = _MutableSlots(1)
    supervisor = ChildProcessSupervisor(
        SupervisorConfig(
            llama_binary=Path("/usr/bin/true"),
            startup_timeout_s=_DEADLINE_S,
            health_poll_interval_s=60,
            stop_grace_s=_DEADLINE_S,
        ),
        _sleeper,
        health_check=_Healthy(),
        slots_check=slots,
    )
    try:
        supervisor.start_replica(_manifest(), Path("/models/tiny.gguf"), 8080)
        assert _wait_for(lambda: supervisor.statuses()[0].inflight == 1)
        old_child = supervisor._children["tiny"]
        supervisor.stop_replica("tiny")

        slots.value = 2
        supervisor.start_replica(_manifest(), Path("/models/tiny.gguf"), 8081)
        assert _wait_for(lambda: supervisor.statuses()[0].inflight == 2)
        assert "tiny" not in supervisor._slot_probe_warned

        slots.value = None
        supervisor._poll_slots(old_child)

        assert "tiny" not in supervisor._slot_probe_warned
        assert supervisor.statuses()[0].inflight == 2
    finally:
        supervisor.stop_all()
