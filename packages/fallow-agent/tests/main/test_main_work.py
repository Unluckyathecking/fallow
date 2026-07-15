"""WorkLoop: happy-path lease→run→complete, and pausing while ACTIVE."""

from __future__ import annotations

from pathlib import Path

import pytest
from main_helpers import FakePreemptor, fixed_now, lease, ok_result

from fallow_agent.heartbeat import CoordinatorProtocolError
from fallow_agent.main.shared import LeaseRegistry
from fallow_agent.main.work import WorkLoop
from fallow_agent.workers import DeferredWorkResult
from fallow_protocol.messages import AgentState, WorkResult, WorkUnitLease


class FakeClient:
    """Duck-typed CoordinatorClient: canned poll results + completion log."""

    def __init__(self, leases: list[WorkUnitLease | None]) -> None:
        self._leases = leases
        self.polls = 0
        self.completed: list[WorkResult] = []

    async def poll_work(self, timeout_s: float) -> WorkUnitLease | None:
        self.polls += 1
        return self._leases.pop(0) if self._leases else None

    async def complete_unit(self, result: WorkResult, *, lease_attempt: int) -> None:
        del lease_attempt
        self.completed.append(result)


class AttemptClient(FakeClient):
    def __init__(self, leases: list[WorkUnitLease | None]) -> None:
        super().__init__(leases)
        self.completed_attempts: list[int] = []

    async def complete_unit(self, result: WorkResult, *, lease_attempt: int) -> None:
        self.completed.append(result)
        self.completed_attempts.append(lease_attempt)


class RejectingCompletionClient(FakeClient):
    async def complete_unit(self, result: WorkResult, *, lease_attempt: int) -> None:
        del result, lease_attempt
        raise CoordinatorProtocolError("unexpected coordinator status 409")


class FakeRunner:
    def __init__(self) -> None:
        self.ran: list[str] = []

    async def run_lease(self, unit: WorkUnitLease) -> WorkResult:
        self.ran.append(unit.work_unit_id)
        return ok_result(unit.work_unit_id)


class DeferredRunner:
    async def run_lease(self, unit: WorkUnitLease) -> DeferredWorkResult:
        return DeferredWorkResult(
            work_unit_id=unit.work_unit_id, payload_path=Path("/tmp/unit-1.1.bin")
        )


class RecordingSleep:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, _seconds: float) -> None:
        self.calls += 1


def _work_loop(client: object, runner: object, preemptor: FakePreemptor, sleep: object) -> WorkLoop:
    return WorkLoop(
        client=client,  # type: ignore[arg-type]
        runner=runner,  # type: ignore[arg-type]
        preemptor=preemptor,
        leases=LeaseRegistry(),
        poll_timeout_s=20.0,
        active_sleep_s=1.0,
        now=fixed_now,
        sleep=sleep,  # type: ignore[arg-type]
    )


async def test_happy_path_lease_run_complete() -> None:
    client = AttemptClient([lease()])
    runner = FakeRunner()
    loop = _work_loop(client, runner, FakePreemptor(AgentState.IDLE), RecordingSleep())

    await loop.tick()

    assert client.polls == 1
    assert runner.ran == ["unit-1"]
    assert [r.work_unit_id for r in client.completed] == ["unit-1"]
    assert client.completed_attempts == [1]


async def test_lease_id_cleared_after_unit() -> None:
    client = FakeClient([lease()])
    runner = FakeRunner()
    leases = LeaseRegistry()
    loop = WorkLoop(
        client=client,  # type: ignore[arg-type]
        runner=runner,  # type: ignore[arg-type]
        preemptor=FakePreemptor(AgentState.IDLE),
        leases=leases,
        poll_timeout_s=20.0,
        active_sleep_s=1.0,
        now=fixed_now,
        sleep=RecordingSleep(),  # type: ignore[arg-type]
    )
    await loop.tick()
    assert leases.current() == ()  # cleared in the finally block


async def test_deferred_upload_reports_no_completion() -> None:
    client = FakeClient([lease()])
    loop = _work_loop(client, DeferredRunner(), FakePreemptor(AgentState.IDLE), RecordingSleep())

    await loop.tick()

    assert client.completed == []


async def test_stale_completion_is_logged_and_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = RejectingCompletionClient([lease()])
    loop = _work_loop(client, FakeRunner(), FakePreemptor(AgentState.IDLE), RecordingSleep())

    await loop.tick()

    assert "unit unit-1 completion failed: unexpected coordinator status 409" in caplog.text


async def test_pauses_while_active() -> None:
    client = FakeClient([lease()])
    runner = FakeRunner()
    sleep = RecordingSleep()
    loop = _work_loop(client, runner, FakePreemptor(AgentState.ACTIVE), sleep)

    await loop.tick()

    assert client.polls == 0  # never polled while the user is present
    assert runner.ran == []
    assert sleep.calls == 1  # cheap active-sleep instead
