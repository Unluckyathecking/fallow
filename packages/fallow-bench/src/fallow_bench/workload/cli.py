"""Command-line entry for the workload generator (``python -m fallow_bench``).

This is the *only* place that reads the wall clock and the environment: it
resolves the API/admin keys from the env-var names in the config, builds real
``httpx.AsyncClient``s pointed at the coordinator, and runs the arm. Everything
below receives injected clocks and clients and is deterministic under test.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Sequence
from pathlib import Path

import httpx

from fallow_bench.workload.clocks import Clocks
from fallow_bench.workload.config import ExperimentConfig, load_config
from fallow_bench.workload.runner import WorkloadRunner

_PROG = "fallow_bench"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=_PROG, description="Fallow workload generator.")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="drive one experiment arm from a YAML config.")
    run.add_argument("--config", type=Path, required=True, help="path to the experiment YAML.")
    run.add_argument("--out", type=Path, required=True, help="run output directory.")
    return parser


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"environment variable {name!r} is not set")
    return value


async def _execute(config: ExperimentConfig, base_dir: Path, out_dir: Path) -> None:
    api_key = _require_env(config.api_key_env)
    admin_key = _require_env(config.sampling.admin_key_env)
    base_url = config.coordinator_url
    async with (
        httpx.AsyncClient(base_url=base_url) as interactive_client,
        httpx.AsyncClient(base_url=base_url) as admin_client,
    ):
        runner = WorkloadRunner(
            config=config,
            base_dir=base_dir,
            out_dir=out_dir,
            interactive_client=interactive_client,
            admin_client=admin_client,
            api_key=api_key,
            admin_key=admin_key,
            clocks=Clocks(),
        )
        await runner.run()


def execute_run(config_path: Path, out_dir: Path) -> None:
    """Load a config and drive one arm. Shared by the standalone CLI and the
    top-level ``python -m fallow_bench run`` dispatcher."""
    config = load_config(config_path)
    asyncio.run(_execute(config, config_path.parent, out_dir))


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    execute_run(args.config, args.out)
    return 0
