from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from fallow_bench.experiment.layout import RunLayout, create_run_layout
from fallow_bench.experiment.models import ArmName, RunMode, RunSpec
from fallow_bench.experiment.plan import DEFAULT_REPETITIONS, build_plan

RunCallback = Callable[[RunSpec, RunLayout], Awaitable[None]]
RunFactory = Callable[[Path], RunCallback]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fallow_bench experiment",
        description="Run the canonical Fallow scheduling experiment.",
    )
    parser.add_argument("--out", required=True, type=Path, help="experiment output root")
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="use the 120-second smoke plan instead of two-hour runs",
    )
    parser.add_argument(
        "--arm",
        choices=tuple(ArmName),
        type=ArmName,
        help="run only one scheduler arm",
    )
    parser.add_argument(
        "--repetition",
        choices=range(1, DEFAULT_REPETITIONS + 1),
        type=int,
        help="run only one repetition",
    )
    return parser


def select_plan(
    *,
    smoke: bool,
    arm: ArmName | None = None,
    repetition: int | None = None,
) -> tuple[RunSpec, ...]:
    """Filter the canonical plan without rebuilding or reordering it."""
    mode = RunMode.SMOKE if smoke else RunMode.LIVE
    return tuple(
        spec
        for spec in build_plan(mode)
        if (arm is None or spec.arm.name is arm)
        and (repetition is None or spec.repetition == repetition)
    )


async def execute_plan(
    root: Path,
    plan: Sequence[RunSpec],
    run: RunCallback,
) -> tuple[RunLayout, ...]:
    """Allocate and execute runs sequentially in canonical plan order."""
    layouts: list[RunLayout] = []
    for spec in plan:
        layout = create_run_layout(root, spec)
        await run(spec, layout)
        layouts.append(layout)
    return tuple(layouts)


def _missing_runner_factory(_root: Path) -> RunCallback:
    async def missing_runner(_spec: RunSpec, _layout: RunLayout) -> None:
        raise RuntimeError("experiment runtime adapter is not configured")

    return missing_runner


def main(
    argv: Sequence[str] | None = None,
    *,
    runner_factory: RunFactory | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    plan = select_plan(smoke=args.smoke, arm=args.arm, repetition=args.repetition)
    factory = runner_factory or _missing_runner_factory
    run = factory(args.out)
    asyncio.run(execute_plan(args.out, plan, run))
    return 0
