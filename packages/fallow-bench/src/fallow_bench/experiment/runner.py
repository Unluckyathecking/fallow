from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Protocol

from fallow_bench.experiment.layout import RunLayout, create_run_layout
from fallow_bench.experiment.models import RunSpec
from fallow_bench.workload.runner import RunMetadata


class RunPhase(Protocol):
    async def __call__(self, *, spec: RunSpec, layout: RunLayout) -> None: ...


Now = Callable[[], datetime]


class ExperimentRunner:
    def __init__(
        self,
        *,
        root: Path,
        baseline: RunPhase,
        workload: RunPhase,
        churn: RunPhase,
        cleanup: RunPhase,
        now: Now,
        config_digest: str,
        git_sha: str,
    ) -> None:
        self._root = root
        self._baseline = baseline
        self._workload = workload
        self._churn = churn
        self._cleanup = cleanup
        self._now = now
        self._config_digest = config_digest
        self._git_sha = git_sha

    async def run(self, spec: RunSpec, *, layout: RunLayout | None = None) -> RunLayout:
        resolved = layout or create_run_layout(self._root, spec)
        metadata = RunMetadata(
            started_at=self._now(),
            arm_label=spec.arm.name,
            rep=spec.repetition,
            seed=spec.seed,
            duration_s=spec.duration_s,
            config_digest=self._config_digest,
            git_sha=self._git_sha,
        )
        resolved.run_meta.write_text(metadata.model_dump_json(indent=2), encoding="utf-8")
        resolved.power.touch(exist_ok=True)
        if not spec.arm.churn_enabled:
            resolved.churn.touch(exist_ok=True)
        try:
            await self._baseline(spec=spec, layout=resolved)
            phases = [self._workload(spec=spec, layout=resolved)]
            if spec.arm.churn_enabled:
                phases.append(self._churn(spec=spec, layout=resolved))
            tasks = [asyncio.create_task(phase) for phase in phases]
            try:
                await asyncio.gather(*tasks)
            except BaseException:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
        except BaseException:
            await self._cleanup(spec=spec, layout=resolved)
            raise
        return resolved
