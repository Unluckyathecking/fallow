from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fallow_bench.analysis import RUN_FILES
from fallow_bench.experiment.models import RunSpec


@dataclass(frozen=True)
class RunLayout:
    directory: Path
    coordinator_config: Path
    database: Path
    blobs: Path
    unit_inputs: Path
    results: Path
    run_meta: Path
    client_trace: Path
    gateway: Path
    events: Path
    churn: Path
    power: Path
    units: Path
    schedule: Path
    jobs: Path

    @property
    def artifacts(self) -> tuple[Path, ...]:
        return (
            self.coordinator_config,
            self.run_meta,
            self.client_trace,
            self.gateway,
            self.events,
            self.churn,
            self.power,
            self.units,
            self.schedule,
            self.jobs,
        )


def create_run_layout(root: Path, run: RunSpec) -> RunLayout:
    directory = root / run.arm.name / f"rep-{run.repetition:02d}"
    directory.mkdir(parents=True, exist_ok=False)
    return RunLayout(
        directory=directory,
        coordinator_config=directory / "coordinator.toml",
        database=directory / "coordinator.db",
        blobs=directory / "blobs",
        unit_inputs=directory / "unit-inputs",
        results=directory / "results",
        run_meta=directory / RUN_FILES.run_meta,
        client_trace=directory / RUN_FILES.client_trace,
        gateway=directory / RUN_FILES.gateway,
        events=directory / RUN_FILES.events,
        churn=directory / RUN_FILES.churn,
        power=directory / RUN_FILES.power,
        units=directory / RUN_FILES.units,
        schedule=directory / "schedule.jsonl",
        jobs=directory / "jobs.jsonl",
    )
