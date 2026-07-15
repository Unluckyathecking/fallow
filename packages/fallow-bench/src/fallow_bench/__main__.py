"""``python -m fallow_bench`` entry point.

Exposes two subcommands. ``churn`` (B2) loads a churn config (standalone or a B1
experiment YAML with a ``churn:`` section), builds the seeded (or scripted)
schedule, and replays it against the live fleet, writing ``churn.jsonl`` under
``--out`` — the only place real time, real HTTP, and real subprocesses are wired
in. ``analyze`` (B3) is the offline reducer: run directories in, ``report.md`` /
``report.tex`` / plots out; it is delegated to
:func:`fallow_bench.analysis.cli.main` and stays fully deterministic.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

import httpx

from fallow_bench.analysis.cli import main as analyze_main
from fallow_bench.churn import constants as k
from fallow_bench.churn.config import load_churn_section
from fallow_bench.churn.injector import ChurnInjector
from fallow_bench.churn.models import ChurnSection
from fallow_bench.churn.runner import run_shell
from fallow_bench.churn.schedule import resolve_schedule
from fallow_bench.churn.writer import ChurnLog
from fallow_bench.workload.cli import execute_run

_ANALYZE = "analyze"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="fallow_bench")
    sub = parser.add_subparsers(dest="command", required=True)
    churn = sub.add_parser("churn", help="replay a fleet-churn schedule")
    churn.add_argument("--config", required=True, type=Path, help="churn/experiment YAML")
    churn.add_argument("--out", required=True, type=Path, help="output directory")
    run = sub.add_parser("run", help="drive one experiment arm (workload generator)")
    run.add_argument("--config", required=True, type=Path, help="experiment YAML")
    run.add_argument("--out", required=True, type=Path, help="output directory")
    return parser.parse_args(argv)


async def _run_churn(section: ChurnSection, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    log = ChurnLog(out_dir / k.CHURN_JSONL_NAME)
    agents = {agent.name: agent for agent in section.agents}
    schedule = resolve_schedule(section)
    async with httpx.AsyncClient(timeout=k.HTTP_TIMEOUT_S) as client:
        injector = ChurnInjector(
            client=client,
            runner=run_shell,
            sink=log.write,
            clock=time.monotonic,
            sleep=asyncio.sleep,
            agents=agents,
            commands=section.commands,
            verify=section.verify,
        )
        await injector.run(schedule)


def main(argv: list[str] | None = None) -> None:
    args_list = sys.argv[1:] if argv is None else argv
    # B3 `analyze` owns its own argument parser; delegate before argparse below.
    if args_list and args_list[0] == _ANALYZE:
        raise SystemExit(analyze_main(args_list[1:]))
    args = _parse_args(args_list)
    if args.command == "churn":
        section = load_churn_section(args.config)
        asyncio.run(_run_churn(section, args.out))
    elif args.command == "run":
        execute_run(args.config, args.out)


if __name__ == "__main__":  # pragma: no cover
    main()
