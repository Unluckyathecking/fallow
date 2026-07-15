"""Power/state sampler: the software-side energy trace.

Polls ``GET /v1/admin/agents`` at ``admin_poll_hz`` and writes one
:class:`PowerSample` per (agent, GPU) to ``power.jsonl``. Agents with no GPU
emit a single null-GPU row so their state is still tracked. The sampler runs
until the injected stop event is set (by the runner, after the interactive and
batch drivers finish). Transient admin errors are swallowed so a blip never
kills the trace.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import datetime

import httpx

from fallow_bench.workload.admin import BenchAdminClient
from fallow_bench.workload.clocks import Clocks
from fallow_bench.workload.records import PowerSample
from fallow_bench.workload.writer import JsonlWriter
from fallow_protocol import AgentSnapshot


def _samples_for(agent: AgentSnapshot, t: datetime) -> Iterator[PowerSample]:
    if not agent.gpus:
        yield PowerSample(
            t=t,
            agent_id=agent.agent_id,
            state=str(agent.state),
            gpu_index=None,
            power_w=None,
            util_percent=None,
            vram_free_mb=None,
        )
        return
    for gpu in agent.gpus:
        yield PowerSample(
            t=t,
            agent_id=agent.agent_id,
            state=str(agent.state),
            gpu_index=gpu.index,
            power_w=gpu.power_w,
            util_percent=gpu.util_percent,
            vram_free_mb=gpu.vram_free_mb,
        )


class PowerSampler:
    """Periodically snapshots agent power/state into ``power.jsonl``."""

    def __init__(
        self,
        *,
        admin: BenchAdminClient,
        poll_hz: float,
        clocks: Clocks,
        writer: JsonlWriter,
    ) -> None:
        self._admin = admin
        self._interval_s = 1.0 / poll_hz
        self._clocks = clocks
        self._writer = writer

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self._sample_once()
            await self._clocks.sleep(self._interval_s)

    async def _sample_once(self) -> None:
        try:
            agents = await self._admin.list_agents()
        except httpx.HTTPError:
            return
        t = self._clocks.now()
        for agent in agents:
            for sample in _samples_for(agent, t):
                self._writer.write(sample)
