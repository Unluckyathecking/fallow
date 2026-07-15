from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fallow_bench.experiment import (
    ExperimentRunner,
    RunLayout,
    RunMode,
    RunPhase,
    RunSpec,
    build_plan,
    create_run_layout,
)


async def _idle_phase(*, spec: RunSpec, layout: RunLayout) -> None:
    del spec, layout


def _runner(tmp_path: Path, **phases: RunPhase) -> ExperimentRunner:
    return ExperimentRunner(
        root=tmp_path,
        baseline=phases.get("baseline", _idle_phase),
        workload=phases.get("workload", _idle_phase),
        churn=phases.get("churn", _idle_phase),
        cleanup=phases.get("cleanup", _idle_phase),
        now=lambda: datetime(2026, 7, 15, 12, 34, 56, tzinfo=UTC),
        config_digest="a" * 64,
        git_sha="deadbeef",
    )


@pytest.mark.asyncio
async def test_e13_writes_canonical_metadata_before_baseline_activity(tmp_path: Path) -> None:
    observed: list[str] = []

    async def baseline(*, spec: RunSpec, layout: RunLayout) -> None:
        del spec
        observed.append("baseline")
        assert (tmp_path / "round_robin" / "rep-01" / "run_meta.json").is_file()
        power = layout.power
        assert power.read_text(encoding="utf-8") == ""
        power.write_text(
            json.dumps(
                {
                    "t": "2026-07-15T12:34:56Z",
                    "agent_id": "smoke-agent",
                    "state": "IDLE",
                    "gpu_index": 0,
                    "power_w": 42.0,
                    "util_percent": 0.0,
                    "vram_free_mb": 1024,
                }
            )
            + "\n",
            encoding="utf-8",
        )

    async def workload(*, spec: RunSpec, layout: RunLayout) -> None:
        del spec, layout
        observed.append("workload")

    async def churn(*, spec: RunSpec, layout: RunLayout) -> None:
        del spec, layout
        observed.append("churn")

    async def cleanup(*, spec: RunSpec, layout: RunLayout) -> None:
        del spec, layout
        observed.append("cleanup")

    run = build_plan(RunMode.SMOKE)[3]
    runner = ExperimentRunner(
        root=tmp_path,
        baseline=baseline,
        workload=workload,
        churn=churn,
        cleanup=cleanup,
        now=lambda: datetime(2026, 7, 15, 12, 34, 56, tzinfo=UTC),
        config_digest="a" * 64,
        git_sha="deadbeef",
    )

    layout = await runner.run(run)

    assert json.loads(layout.run_meta.read_text(encoding="utf-8")) == {
        "started_at": "2026-07-15T12:34:56Z",
        "arm_label": "round_robin",
        "rep": 1,
        "seed": 17,
        "duration_s": 120.0,
        "config_digest": "a" * 64,
        "git_sha": "deadbeef",
    }
    assert observed == ["baseline", "workload", "churn"]
    assert json.loads(layout.power.read_text(encoding="utf-8"))["power_w"] == 42.0


@pytest.mark.asyncio
async def test_e13_accepts_layout_and_writes_empty_churn_for_dedicated(tmp_path: Path) -> None:
    churn_called = False

    async def churn(*, spec: RunSpec, layout: RunLayout) -> None:
        del spec, layout
        nonlocal churn_called
        churn_called = True

    run = build_plan(RunMode.SMOKE)[0]
    layout = create_run_layout(tmp_path, run)

    result = await _runner(tmp_path, churn=churn).run(run, layout=layout)

    assert result is layout
    assert layout.churn.read_text(encoding="utf-8") == ""
    assert churn_called is False


@pytest.mark.asyncio
async def test_e13_starts_workload_and_churn_concurrently_after_baseline(tmp_path: Path) -> None:
    baseline_done = False
    workload_started = asyncio.Event()
    churn_started = asyncio.Event()

    async def baseline(*, spec: RunSpec, layout: RunLayout) -> None:
        del spec, layout
        nonlocal baseline_done
        baseline_done = True

    async def workload(*, spec: RunSpec, layout: RunLayout) -> None:
        del spec, layout
        assert baseline_done
        workload_started.set()
        await churn_started.wait()

    async def churn(*, spec: RunSpec, layout: RunLayout) -> None:
        del spec, layout
        assert baseline_done
        churn_started.set()
        await workload_started.wait()

    run = build_plan(RunMode.SMOKE)[3]

    await _runner(tmp_path, baseline=baseline, workload=workload, churn=churn).run(run)

    assert workload_started.is_set()
    assert churn_started.is_set()


@pytest.mark.asyncio
async def test_e13_cancels_sibling_and_cleans_up_on_phase_failure(tmp_path: Path) -> None:
    churn_started = asyncio.Event()
    churn_cancelled = False
    cleanup_called = False

    async def workload(*, spec: RunSpec, layout: RunLayout) -> None:
        del spec, layout
        await churn_started.wait()
        raise RuntimeError("workload failed")

    async def churn(*, spec: RunSpec, layout: RunLayout) -> None:
        del spec, layout
        nonlocal churn_cancelled
        churn_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            churn_cancelled = True
            raise

    async def cleanup(*, spec: RunSpec, layout: RunLayout) -> None:
        del spec, layout
        nonlocal cleanup_called
        cleanup_called = True

    run = build_plan(RunMode.SMOKE)[3]

    with pytest.raises(RuntimeError, match="workload failed"):
        await _runner(tmp_path, workload=workload, churn=churn, cleanup=cleanup).run(run)

    assert churn_cancelled is True
    assert cleanup_called is True


@pytest.mark.asyncio
async def test_e13_cleans_up_when_baseline_fails(tmp_path: Path) -> None:
    cleanup_called = False

    async def baseline(*, spec: RunSpec, layout: RunLayout) -> None:
        del spec, layout
        raise RuntimeError("baseline failed")

    async def cleanup(*, spec: RunSpec, layout: RunLayout) -> None:
        del spec, layout
        nonlocal cleanup_called
        cleanup_called = True

    run = build_plan(RunMode.SMOKE)[3]

    with pytest.raises(RuntimeError, match="baseline failed"):
        await _runner(tmp_path, baseline=baseline, cleanup=cleanup).run(run)

    assert cleanup_called is True
