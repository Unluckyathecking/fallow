from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from fallow_bench.experiment.cli import build_parser, execute_plan, main, select_plan
from fallow_bench.experiment.layout import RunLayout
from fallow_bench.experiment.models import ArmName, RunMode, RunSpec


def test_e13_experiment_parser_accepts_filters() -> None:
    args = build_parser().parse_args(
        [
            "--out",
            "runs",
            "--smoke",
            "--arm",
            "round_robin",
            "--repetition",
            "2",
        ]
    )

    assert args.out == Path("runs")
    assert args.smoke is True
    assert args.arm is ArmName.ROUND_ROBIN
    assert args.repetition == 2


def test_e13_experiment_filters_canonical_plan_without_changing_seed() -> None:
    plan = select_plan(smoke=True, arm=ArmName.CHURN_V2, repetition=3)

    assert len(plan) == 1
    assert plan[0].arm.name is ArmName.CHURN_V2
    assert plan[0].repetition == 3
    assert plan[0].seed == 43
    assert plan[0].duration_s == 120
    assert plan[0].mode is RunMode.SMOKE


def test_e13_experiment_preserves_canonical_order_and_paired_seeds() -> None:
    plan = select_plan(smoke=False, repetition=2)

    assert [(spec.arm.name, spec.repetition, spec.seed) for spec in plan] == [
        (ArmName.DEDICATED, 2, 29),
        (ArmName.ROUND_ROBIN, 2, 29),
        (ArmName.CHURN_V2, 2, 29),
    ]


@pytest.mark.asyncio
async def test_e13_experiment_executes_selected_runs_sequentially(tmp_path: Path) -> None:
    active = 0
    observed: list[tuple[str, int, Path]] = []

    async def run(spec: RunSpec, layout: RunLayout) -> None:
        nonlocal active
        assert active == 0
        active += 1
        observed.append((str(spec.arm.name), spec.repetition, layout.directory))
        active -= 1

    plan = select_plan(smoke=True, repetition=1)
    layouts = await execute_plan(tmp_path, plan, run)

    assert observed == [
        ("dedicated", 1, tmp_path / "dedicated" / "rep-01"),
        ("round_robin", 1, tmp_path / "round_robin" / "rep-01"),
        ("churn_v2", 1, tmp_path / "churn_v2" / "rep-01"),
    ]
    assert tuple(layout.directory for layout in layouts) == tuple(row[2] for row in observed)


@pytest.mark.asyncio
async def test_e13_experiment_surfaces_run_directory_collision(tmp_path: Path) -> None:
    async def run(_spec: RunSpec, _layout: RunLayout) -> None:
        return None

    plan = select_plan(smoke=True, arm=ArmName.DEDICATED, repetition=1)
    await execute_plan(tmp_path, plan, run)

    with pytest.raises(FileExistsError):
        await execute_plan(tmp_path, plan, run)


def test_e13_experiment_main_builds_one_runner_and_executes_plan(tmp_path: Path) -> None:
    factory_roots: list[Path] = []
    observed: list[tuple[ArmName, int]] = []

    def factory(root: Path) -> Callable[[RunSpec, RunLayout], Awaitable[None]]:
        factory_roots.append(root)

        async def run(spec: RunSpec, _layout: RunLayout) -> None:
            observed.append((spec.arm.name, spec.repetition))

        return run

    result = main(
        [
            "--out",
            str(tmp_path),
            "--smoke",
            "--arm",
            "round_robin",
            "--repetition",
            "2",
        ],
        runner_factory=factory,
    )

    assert result == 0
    assert factory_roots == [tmp_path]
    assert observed == [(ArmName.ROUND_ROBIN, 2)]


def test_e13_top_level_dispatches_experiment_command(tmp_path: Path) -> None:
    from fallow_bench.__main__ import main as dispatch

    observed: list[tuple[ArmName, int]] = []

    def factory(_root: Path) -> Callable[[RunSpec, RunLayout], Awaitable[None]]:
        async def run(spec: RunSpec, _layout: RunLayout) -> None:
            observed.append((spec.arm.name, spec.repetition))

        return run

    with pytest.raises(SystemExit) as exit_info:
        dispatch(
            [
                "experiment",
                "--out",
                str(tmp_path),
                "--smoke",
                "--arm",
                "dedicated",
                "--repetition",
                "1",
            ],
            experiment_runner_factory=factory,
        )

    assert exit_info.value.code == 0
    assert observed == [(ArmName.DEDICATED, 1)]
