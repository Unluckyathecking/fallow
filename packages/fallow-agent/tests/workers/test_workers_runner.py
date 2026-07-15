"""Unit tests for WorkUnitRunner: happy path and every failure mode."""

from workers_helpers import FakeClock, FakeWorker, make_lease

from fallow_agent.workers import WorkOutput, WorkUnitRunner
from fallow_protocol.capabilities import WorkerKind
from fallow_protocol.messages import WorkMetrics, WorkResultStatus, WorkUnitLease


class FetchFailed(Exception):
    """Typed error a fake fetch raises to prove the message survives."""


def _fetch_ok(_lease: WorkUnitLease) -> bytes:
    return b"input-bytes"


async def _fetch_ok_async(lease: WorkUnitLease) -> bytes:
    return _fetch_ok(lease)


async def test_runner_happy_path_records_success_and_duration() -> None:
    uploads: list[tuple[str, bytes]] = []

    async def upload(lease: WorkUnitLease, payload: bytes) -> str:
        uploads.append((lease.work_unit_id, payload))
        return "result-ref-1"

    worker = FakeWorker(
        output=WorkOutput(payload=b"out", metrics=WorkMetrics(duration_s=0.0, items=3))
    )
    runner = WorkUnitRunner(
        workers={WorkerKind.EMBED: worker},
        fetch_input=_fetch_ok_async,
        upload=upload,
        monotonic=FakeClock([1.0, 1.5]),
    )

    result = await runner.run_lease(make_lease())

    assert result.status is WorkResultStatus.SUCCEEDED
    assert result.result_ref == "result-ref-1"
    assert result.error is None
    assert result.metrics is not None
    assert result.metrics.items == 3
    assert result.metrics.duration_s == 0.5
    assert worker.calls == [("unit-1", b"input-bytes")]
    assert uploads == [("unit-1", b"out")]


async def test_runner_worker_raises_becomes_failed() -> None:
    worker = FakeWorker(raises=RuntimeError("boom"))

    async def upload(_lease: WorkUnitLease, _payload: bytes) -> str:
        raise AssertionError("upload must not run when the worker raised")

    runner = WorkUnitRunner(
        workers={WorkerKind.EMBED: worker},
        fetch_input=_fetch_ok_async,
        upload=upload,
        monotonic=FakeClock([2.0, 2.25]),
    )

    result = await runner.run_lease(make_lease())

    assert result.status is WorkResultStatus.FAILED
    assert result.result_ref is None
    assert result.error == "RuntimeError: boom"
    assert result.metrics is not None
    assert result.metrics.duration_s == 0.25


async def test_runner_fetch_failure_is_failed_with_typed_message() -> None:
    async def fetch(_lease: WorkUnitLease) -> bytes:
        raise FetchFailed("input gone")

    async def upload(_lease: WorkUnitLease, _payload: bytes) -> str:
        raise AssertionError("upload must not run when fetch failed")

    runner = WorkUnitRunner(
        workers={WorkerKind.EMBED: FakeWorker()},
        fetch_input=fetch,
        upload=upload,
        monotonic=FakeClock([0.0, 0.1]),
    )

    result = await runner.run_lease(make_lease())

    assert result.status is WorkResultStatus.FAILED
    assert result.error == "FetchFailed: input gone"


async def test_runner_unknown_kind_is_failed() -> None:
    async def upload(_lease: WorkUnitLease, _payload: bytes) -> str:
        return "ref"

    runner = WorkUnitRunner(
        workers={},
        fetch_input=_fetch_ok_async,
        upload=upload,
        monotonic=FakeClock([0.0, 0.0]),
    )

    result = await runner.run_lease(make_lease(kind=WorkerKind.TRANSCRIBE))

    assert result.status is WorkResultStatus.FAILED
    assert result.error is not None
    assert "WorkerNotRegisteredError" in result.error


async def test_runner_upload_failure_is_failed() -> None:
    async def upload(_lease: WorkUnitLease, _payload: bytes) -> str:
        raise ConnectionError("coordinator unreachable")

    runner = WorkUnitRunner(
        workers={WorkerKind.EMBED: FakeWorker()},
        fetch_input=_fetch_ok_async,
        upload=upload,
        monotonic=FakeClock([0.0, 0.3]),
    )

    result = await runner.run_lease(make_lease())

    assert result.status is WorkResultStatus.FAILED
    assert result.error == "ConnectionError: coordinator unreachable"
