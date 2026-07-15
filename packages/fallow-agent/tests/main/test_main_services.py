"""AgentServices: startup order and the graceful drain→stop_all shutdown order."""

from __future__ import annotations

from main_helpers import FakePreemptor, FakeSupervisor

from fallow_agent.main.services import AgentServices


class RecordingAsyncLoop:
    def __init__(self, log: list[str], name: str) -> None:
        self._log = log
        self._name = name

    def start(self) -> None:
        self._log.append(f"start:{self._name}")

    def request_stop(self) -> None:
        self._log.append(f"request_stop:{self._name}")

    async def stop(self) -> None:
        self._log.append(f"stop:{self._name}")


class RecordingHeartbeat:
    def __init__(self, log: list[str]) -> None:
        self._log = log

    def start(self) -> None:
        self._log.append("start:heartbeat")

    async def stop(self) -> None:
        self._log.append("stop:heartbeat")


class RecordingPollLoop:
    def __init__(self, log: list[str]) -> None:
        self._log = log

    def start(self) -> None:
        self._log.append("start:poll")

    def stop(self, timeout: float | None = None) -> None:
        self._log.append("stop:poll")


class RecordingSink:
    def __init__(self, log: list[str]) -> None:
        self._log = log

    def start(self) -> None:
        self._log.append("start:sink")

    async def stop(self) -> None:
        self._log.append("stop:sink")


def _services(log: list[str]) -> AgentServices:
    async def _final() -> None:
        log.append("final_heartbeat")

    return AgentServices(
        preemptor=FakePreemptor(log=log),
        supervisor=FakeSupervisor(log=log),
        event_sink=RecordingSink(log),
        poll_loop=RecordingPollLoop(log),
        heartbeat=RecordingHeartbeat(log),
        reconcile_loop=RecordingAsyncLoop(log, "reconcile"),
        work_loop=RecordingAsyncLoop(log, "work"),
        final_heartbeat=_final,
    )


def test_start_order() -> None:
    log: list[str] = []
    _services(log).start()
    assert log == [
        "start:sink",
        "start:poll",
        "start:heartbeat",
        "start:reconcile",
        "start:work",
    ]


async def test_graceful_shutdown_order() -> None:
    log: list[str] = []
    await _services(log).stop()

    # Drain is first; stop_all and the sink flush are last.
    assert log[0] == "drain"
    assert log[-1] == "stop:sink"
    assert log.index("drain") < log.index("stop_all")
    assert log.index("stop_all") < log.index("stop:sink")

    # Work + reconcile loops are stopped before the final heartbeat and stop_all.
    assert log.index("stop:work") < log.index("final_heartbeat")
    assert log.index("stop:reconcile") < log.index("final_heartbeat")
    assert log.index("final_heartbeat") < log.index("stop_all")
    # The periodic heartbeat is stopped before the final explicit beat.
    assert log.index("stop:heartbeat") < log.index("final_heartbeat")


async def test_final_heartbeat_failure_does_not_abort_shutdown() -> None:
    log: list[str] = []

    async def _boom() -> None:
        log.append("final_heartbeat")
        raise RuntimeError("coordinator unreachable")

    services = AgentServices(
        preemptor=FakePreemptor(log=log),
        supervisor=FakeSupervisor(log=log),
        event_sink=RecordingSink(log),
        poll_loop=RecordingPollLoop(log),
        heartbeat=RecordingHeartbeat(log),
        reconcile_loop=RecordingAsyncLoop(log, "reconcile"),
        work_loop=RecordingAsyncLoop(log, "work"),
        final_heartbeat=_boom,
    )
    await services.stop()  # must not raise
    assert "stop_all" in log  # shutdown continued past the failed beat
