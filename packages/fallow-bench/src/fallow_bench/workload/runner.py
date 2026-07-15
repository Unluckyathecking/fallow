"""Run orchestration: wire the three drivers over one run directory.

Builds the fixed schedule and prompt corpus, opens the three JSONL writers,
dumps the resolved schedule + run metadata for reproducibility, then runs the
interactive and batch drivers to completion while the power sampler runs
concurrently. All clocks and HTTP clients are injected; ``__main__`` supplies
the real ones.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

import httpx

from fallow_bench.workload.admin import BenchAdminClient
from fallow_bench.workload.batch import BatchDriver
from fallow_bench.workload.clocks import Clocks
from fallow_bench.workload.config import ExperimentConfig
from fallow_bench.workload.corpus import load_prompts
from fallow_bench.workload.interactive import InteractiveDriver
from fallow_bench.workload.sampler import PowerSampler
from fallow_bench.workload.schedule import Arrival, build_schedule
from fallow_bench.workload.writer import JsonlWriter
from fallow_protocol import JobSubmit

_REQUESTS_FILE = "requests.jsonl"
_JOBS_FILE = "jobs.jsonl"
_POWER_FILE = "power.jsonl"
_SCHEDULE_FILE = "schedule.jsonl"
_META_FILE = "run.json"


def _resolve(base_dir: Path, ref: str) -> Path:
    path = Path(ref)
    return path if path.is_absolute() else base_dir / path


def _job_submit(config: ExperimentConfig) -> JobSubmit:
    return JobSubmit(
        kind=config.batch.kind,
        model_id=config.batch.model_id,
        payload_ref=config.batch.corpus_path,
        priority=config.batch.priority,
    )


def _dump_schedule(path: Path, schedule: Sequence[Arrival]) -> None:
    with JsonlWriter(path) as writer:
        for arrival in schedule:
            writer.write(arrival)


def _dump_meta(path: Path, config: ExperimentConfig, n_arrivals: int, n_prompts: int) -> None:
    meta = {
        "arm_label": config.arm_label,
        "coordinator_url": config.coordinator_url,
        "model_id": config.model_id,
        "duration_s": config.duration_s,
        "seed": config.seed,
        "n_arrivals": n_arrivals,
        "n_prompts": n_prompts,
    }
    path.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")


class WorkloadRunner:
    """Owns one experiment-arm run end to end."""

    def __init__(
        self,
        *,
        config: ExperimentConfig,
        base_dir: Path,
        out_dir: Path,
        interactive_client: httpx.AsyncClient,
        admin_client: httpx.AsyncClient,
        api_key: str,
        admin_key: str,
        clocks: Clocks,
    ) -> None:
        self._config = config
        self._base_dir = base_dir
        self._out_dir = out_dir
        self._interactive_client = interactive_client
        self._admin = BenchAdminClient(admin_client, admin_key)
        self._api_key = api_key
        self._clocks = clocks

    async def run(self) -> Path:
        self._out_dir.mkdir(parents=True, exist_ok=True)
        prompts = self._load_prompts()
        schedule = self._build_schedule(len(prompts))
        _dump_schedule(self._out_dir / _SCHEDULE_FILE, schedule)
        _dump_meta(self._out_dir / _META_FILE, self._config, len(schedule), len(prompts))
        with (
            JsonlWriter(self._out_dir / _REQUESTS_FILE) as req_writer,
            JsonlWriter(self._out_dir / _JOBS_FILE) as job_writer,
            JsonlWriter(self._out_dir / _POWER_FILE) as power_writer,
        ):
            await self._drive(prompts, schedule, req_writer, job_writer, power_writer)
        return self._out_dir

    def _load_prompts(self) -> tuple[str, ...]:
        paths = [_resolve(self._base_dir, ref) for ref in self._config.interactive.prompt_files]
        return load_prompts(paths)

    def _build_schedule(self, n_prompts: int) -> tuple[Arrival, ...]:
        return build_schedule(
            seed=self._config.seed,
            rate_per_min=self._config.interactive.rate_per_min,
            duration_s=self._config.duration_s,
            n_prompts=n_prompts,
            max_tokens=self._config.interactive.max_tokens,
        )

    async def _drive(
        self,
        prompts: Sequence[str],
        schedule: Sequence[Arrival],
        req_writer: JsonlWriter,
        job_writer: JsonlWriter,
        power_writer: JsonlWriter,
    ) -> None:
        interactive = self._make_interactive(prompts, schedule, req_writer)
        batch = self._make_batch(job_writer)
        sampler = PowerSampler(
            admin=self._admin,
            poll_hz=self._config.sampling.admin_poll_hz,
            clocks=self._clocks,
            writer=power_writer,
        )
        stop = asyncio.Event()
        sampler_task = asyncio.create_task(sampler.run(stop))
        try:
            await asyncio.gather(interactive.run(), batch.run())
        finally:
            stop.set()
            await sampler_task

    def _make_interactive(
        self,
        prompts: Sequence[str],
        schedule: Sequence[Arrival],
        writer: JsonlWriter,
    ) -> InteractiveDriver:
        return InteractiveDriver(
            client=self._interactive_client,
            api_key=self._api_key,
            model_id=self._config.model_id,
            prompts=prompts,
            schedule=schedule,
            timeout_s=self._config.interactive.request_timeout_s,
            clocks=self._clocks,
            writer=writer,
        )

    def _make_batch(self, writer: JsonlWriter) -> BatchDriver:
        return BatchDriver(
            admin=self._admin,
            job=_job_submit(self._config),
            submit_at_s=self._config.batch.submit_at_s,
            poll_interval_s=self._config.batch.poll_interval_s,
            duration_s=self._config.duration_s,
            clocks=self._clocks,
            writer=writer,
        )
