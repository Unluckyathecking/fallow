"""Async churn injector: replays a schedule against a live bench-mode fleet.

Given a sorted schedule, the injector waits (on an injected clock/sleeper) until
each event's offset, then executes it:

* ``user_return`` → ``POST /simulate_input`` on the target agent, then an
  optional bounded ``GET /state`` poll to time the input→yield flip.
* ``agent_kill`` / ``net_drop`` → a rendered shell command via the injected
  ``Runner``.

Every executed event is recorded to ``churn.jsonl``. A failing endpoint or
runner is logged (``ok=False``) and never aborts the run.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence

import httpx

from fallow_bench.churn import constants as k
from fallow_bench.churn.models import (
    AgentTarget,
    ChurnEvent,
    ChurnKind,
    ChurnRecord,
    RunResult,
    VerifyConfig,
)
from fallow_bench.churn.ports import Clock, RecordSink, Runner, Sleeper
from fallow_bench.churn.verify import measure_flip


class ChurnInjector:
    """Replays a churn schedule against real agents over injected seams."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        runner: Runner,
        sink: RecordSink,
        clock: Clock,
        sleep: Sleeper,
        agents: Mapping[str, AgentTarget],
        commands: Mapping[ChurnKind, str],
        verify: VerifyConfig,
        wall_clock: Clock = time.time,
    ) -> None:
        self._client = client
        self._runner = runner
        self._sink = sink
        self._clock = clock
        self._sleep = sleep
        self._agents = agents
        self._commands = commands
        self._verify = verify
        self._wall_clock = wall_clock

    async def run(self, schedule: Sequence[ChurnEvent]) -> None:
        """Replay the whole schedule; per-event failures are logged, not raised."""
        start = self._clock()
        for event in sorted(schedule, key=lambda e: e.t_offset_s):
            await self._wait_until(start + event.t_offset_s)
            record = await self._execute(event, start)
            await self._sink(record)

    async def _wait_until(self, target_monotonic: float) -> None:
        delay = target_monotonic - self._clock()
        if delay > 0.0:
            await self._sleep(delay)

    async def _execute(self, event: ChurnEvent, start: float) -> ChurnRecord:
        agent = self._agents.get(event.agent_name)
        executed = self._clock() - start
        executed_at = self._wall_clock()
        if agent is None:
            return self._record(event, executed, executed_at, ok=False, detail="unknown agent")
        if event.kind is ChurnKind.USER_RETURN:
            return await self._inject_input(event, agent, executed, executed_at)
        return await self._run_command(event, agent, executed, executed_at)

    async def _inject_input(
        self, event: ChurnEvent, agent: AgentTarget, executed: float, executed_at: float
    ) -> ChurnRecord:
        since = self._clock()
        ok, detail = await self._post_input(agent)
        flip_ms = None
        if ok and self._verify.enabled:
            flip_ms = await measure_flip(
                client=self._client,
                target=agent,
                since=since,
                clock=self._clock,
                sleep=self._sleep,
                config=self._verify,
            )
        return self._record(event, executed, executed_at, ok=ok, detail=detail, flip_ms=flip_ms)

    async def _post_input(self, agent: AgentTarget) -> tuple[bool, str]:
        url = f"http://{agent.host}:{agent.bench_port}{k.SIMULATE_INPUT_PATH}"
        try:
            response = await self._client.post(url)
        except httpx.HTTPError as exc:
            return False, str(exc)
        ok = response.status_code == k.SIMULATE_INPUT_OK_STATUS
        return ok, "" if ok else f"status {response.status_code}"

    async def _run_command(
        self, event: ChurnEvent, agent: AgentTarget, executed: float, executed_at: float
    ) -> ChurnRecord:
        template = self._commands.get(event.kind)
        if template is None:
            return self._record(event, executed, executed_at, ok=False, detail=k.NO_COMMAND_MSG)
        command = template.format(name=agent.name, host=agent.host, bench_port=agent.bench_port)
        result = await self._safe_run(command)
        return self._record(event, executed, executed_at, ok=result.ok, detail=result.detail)

    async def _safe_run(self, command: str) -> RunResult:
        try:
            return await self._runner(command)
        except Exception as exc:  # a broken runner must never abort the run
            return RunResult(ok=False, detail=str(exc))

    def _record(
        self,
        event: ChurnEvent,
        executed: float,
        executed_at: float,
        *,
        ok: bool,
        detail: str = "",
        flip_ms: float | None = None,
    ) -> ChurnRecord:
        return ChurnRecord(
            t=executed_at,
            t_scheduled=event.t_offset_s,
            t_executed=executed,
            agent=event.agent_name,
            kind=event.kind,
            ok=ok,
            detail=detail,
            flip_ms=flip_ms,
        )
