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
        "--config",
        type=Path,
        default=Path("experiments/main.yaml"),
        help="base workload and churn YAML",
    )
    parser.add_argument(
        "--seed-db",
        type=Path,
        help="quiescent full-fleet coordinator database",
    )
    parser.add_argument(
        "--dedicated-seed-db",
        type=Path,
        help="quiescent one-agent coordinator database for the dedicated arm",
    )
    parser.add_argument(
        "--churn-history",
        type=Path,
        help="immutable historical events JSONL used to fit churn_v2",
    )
    parser.add_argument("--host", default="127.0.0.1", help="coordinator bind address")
    parser.add_argument("--port", type=int, default=8080, help="coordinator port")
    parser.add_argument("--revision", help="git commit or immutable build revision")
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


def main(
    argv: Sequence[str] | None = None,
    *,
    runner_factory: RunFactory | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    plan = select_plan(smoke=args.smoke, arm=args.arm, repetition=args.repetition)
    if runner_factory is None:
        from fallow_bench.experiment.live import default_runner_factory

        if (
            any(spec.arm.name is ArmName.DEDICATED for spec in plan)
            and args.dedicated_seed_db is None
        ):
            raise SystemExit("--dedicated-seed-db is required for the dedicated arm")
        if any(spec.arm.name is not ArmName.DEDICATED for spec in plan) and args.seed_db is None:
            raise SystemExit("--seed-db is required for distributed arms")
        if any(spec.arm.name is ArmName.CHURN_V2 for spec in plan) and args.churn_history is None:
            raise SystemExit("--churn-history is required for the churn_v2 arm")
        run = default_runner_factory(
            args.out,
            config_path=args.config,
            seed_database=args.seed_db,
            dedicated_seed_database=args.dedicated_seed_db,
            churn_history=args.churn_history,
            host=args.host,
            port=args.port,
            revision=args.revision,
        )
    else:
        run = runner_factory(args.out)
    asyncio.run(execute_plan(args.out, plan, run))
    return 0
