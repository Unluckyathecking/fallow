"""Injector tests against a fake HTTP transport, fake clock, and fake runner."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from churn_fakes import (
    FakeClock,
    FakeSleeper,
    RecordingRunner,
    RecordingSink,
    input_ok_handler,
    mock_client,
    raising_runner,
)

from fallow_bench.churn import (
    AgentTarget,
    ChurnEvent,
    ChurnInjector,
    ChurnKind,
    ChurnLog,
    ChurnRecord,
    RunResult,
    VerifyConfig,
)
from fallow_bench.churn import (
    constants as k,
)

_MAC = AgentTarget(name="mac", host="10.0.0.1")
_WIN = AgentTarget(name="win", host="10.0.0.2", bench_port=9500)
_AGENTS = {"mac": _MAC, "win": _WIN}
_NO_VERIFY = VerifyConfig(enabled=False)


def _injector(
    *,
    client: httpx.AsyncClient,
    sink: RecordingSink,
    clock: FakeClock,
    sleep: FakeSleeper,
    runner: object = None,
    commands: dict[ChurnKind, str] | None = None,
    verify: VerifyConfig = _NO_VERIFY,
) -> ChurnInjector:
    return ChurnInjector(
        client=client,
        runner=runner if runner is not None else RecordingRunner(),
        sink=sink,
        clock=clock,
        sleep=sleep,
        agents=_AGENTS,
        commands=commands if commands is not None else {},
        verify=verify,
    )


async def test_events_fire_in_order_at_offsets() -> None:
    clock, sink = FakeClock(), RecordingSink()
    sleep = FakeSleeper(clock)
    schedule = [
        ChurnEvent(t_offset_s=3.0, agent_name="win", kind=ChurnKind.USER_RETURN),
        ChurnEvent(t_offset_s=1.0, agent_name="mac", kind=ChurnKind.USER_RETURN),
    ]
    inj = _injector(client=mock_client(input_ok_handler()), sink=sink, clock=clock, sleep=sleep)
    await inj.run(schedule)
    # Sorted by offset; executed offset equals scheduled offset exactly.
    assert [r.agent for r in sink.records] == ["mac", "win"]
    assert [r.t_scheduled for r in sink.records] == [1.0, 3.0]
    assert [r.t_executed for r in sink.records] == [1.0, 3.0]
    assert all(r.ok for r in sink.records)
    assert sleep.delays == [1.0, 2.0]  # slept the gaps, not absolute times


async def test_records_written_to_jsonl(tmp_path: Path) -> None:
    clock = FakeClock()
    sleep = FakeSleeper(clock)
    log = ChurnLog(tmp_path / k.CHURN_JSONL_NAME)
    schedule = [ChurnEvent(t_offset_s=0.0, agent_name="mac", kind=ChurnKind.USER_RETURN)]
    inj = ChurnInjector(
        client=mock_client(input_ok_handler()),
        runner=RecordingRunner(),
        sink=log.write,
        clock=clock,
        sleep=sleep,
        agents=_AGENTS,
        commands={},
        verify=_NO_VERIFY,
    )
    await inj.run(schedule)
    lines = (tmp_path / k.CHURN_JSONL_NAME).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["agent"] == "mac"
    assert row["kind"] == "user_return"
    assert row["ok"] is True
    # round-trips through the frozen model
    assert ChurnRecord.model_validate(row).ok is True


async def test_flip_latency_measured_from_scripted_state() -> None:
    clock, sink = FakeClock(), RecordingSink()
    sleep = FakeSleeper(clock)
    poll_s = 0.05
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/simulate_input"):
            return httpx.Response(204)
        calls["n"] += 1
        state = "active" if calls["n"] >= 3 else "idle"  # active on the 3rd /state
        return httpx.Response(200, json={"state": state, "idle_s": 0.0})

    verify = VerifyConfig(enabled=True, max_wait_s=5.0, poll_interval_s=poll_s)
    inj = _injector(client=mock_client(handler), sink=sink, clock=clock, sleep=sleep, verify=verify)
    await inj.run([ChurnEvent(t_offset_s=0.0, agent_name="mac", kind=ChurnKind.USER_RETURN)])
    record = sink.records[0]
    assert record.ok is True
    # two sleeps of poll_s elapsed before the 3rd poll saw "active".
    assert record.flip_ms == pytest.approx(2 * poll_s * k.MS_PER_S)


async def test_flip_none_when_state_never_flips() -> None:
    clock, sink = FakeClock(), RecordingSink()
    sleep = FakeSleeper(clock)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/simulate_input"):
            return httpx.Response(204)
        return httpx.Response(200, json={"state": "idle", "idle_s": 1.0})

    verify = VerifyConfig(enabled=True, max_wait_s=0.2, poll_interval_s=0.05)
    inj = _injector(client=mock_client(handler), sink=sink, clock=clock, sleep=sleep, verify=verify)
    await inj.run([ChurnEvent(t_offset_s=0.0, agent_name="mac", kind=ChurnKind.USER_RETURN)])
    assert sink.records[0].ok is True
    assert sink.records[0].flip_ms is None  # bounded poll gave up


async def test_survives_http_error_status() -> None:
    clock, sink = FakeClock(), RecordingSink()
    sleep = FakeSleeper(clock)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)  # /simulate_input fails

    inj = _injector(client=mock_client(handler), sink=sink, clock=clock, sleep=sleep)
    await inj.run([ChurnEvent(t_offset_s=0.0, agent_name="mac", kind=ChurnKind.USER_RETURN)])
    assert sink.records[0].ok is False
    assert "500" in sink.records[0].detail


async def test_survives_transport_exception() -> None:
    clock, sink = FakeClock(), RecordingSink()
    sleep = FakeSleeper(clock)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    inj = _injector(client=mock_client(handler), sink=sink, clock=clock, sleep=sleep)
    await inj.run([ChurnEvent(t_offset_s=0.0, agent_name="mac", kind=ChurnKind.USER_RETURN)])
    assert sink.records[0].ok is False  # logged, not raised


async def test_agent_kill_runs_rendered_command() -> None:
    clock, sink = FakeClock(), RecordingSink()
    sleep = FakeSleeper(clock)
    runner = RecordingRunner(RunResult(ok=True, detail="killed"))
    commands = {ChurnKind.AGENT_KILL: "ssh {host} kill {name} {bench_port}"}
    inj = _injector(
        client=mock_client(input_ok_handler()),
        sink=sink,
        clock=clock,
        sleep=sleep,
        runner=runner,
        commands=commands,
    )
    await inj.run([ChurnEvent(t_offset_s=0.0, agent_name="win", kind=ChurnKind.AGENT_KILL)])
    assert runner.commands == ["ssh 10.0.0.2 kill win 9500"]
    assert sink.records[0].ok is True
    assert sink.records[0].detail == "killed"


async def test_missing_command_template_is_logged() -> None:
    clock, sink = FakeClock(), RecordingSink()
    sleep = FakeSleeper(clock)
    runner = RecordingRunner()
    inj = _injector(
        client=mock_client(input_ok_handler()),
        sink=sink,
        clock=clock,
        sleep=sleep,
        runner=runner,
        commands={},  # no template for net_drop
    )
    await inj.run([ChurnEvent(t_offset_s=0.0, agent_name="mac", kind=ChurnKind.NET_DROP)])
    assert runner.commands == []  # never invoked
    assert sink.records[0].ok is False
    assert sink.records[0].detail == k.NO_COMMAND_MSG


async def test_survives_raising_runner() -> None:
    clock, sink = FakeClock(), RecordingSink()
    sleep = FakeSleeper(clock)
    commands = {ChurnKind.AGENT_KILL: "boom {name}"}
    inj = _injector(
        client=mock_client(input_ok_handler()),
        sink=sink,
        clock=clock,
        sleep=sleep,
        runner=raising_runner,
        commands=commands,
    )
    await inj.run([ChurnEvent(t_offset_s=0.0, agent_name="mac", kind=ChurnKind.AGENT_KILL)])
    assert sink.records[0].ok is False
    assert "boom" in sink.records[0].detail


async def test_unknown_agent_is_logged() -> None:
    clock, sink = FakeClock(), RecordingSink()
    sleep = FakeSleeper(clock)
    inj = _injector(client=mock_client(input_ok_handler()), sink=sink, clock=clock, sleep=sleep)
    await inj.run([ChurnEvent(t_offset_s=0.0, agent_name="ghost", kind=ChurnKind.USER_RETURN)])
    assert sink.records[0].ok is False
    assert sink.records[0].detail == "unknown agent"
