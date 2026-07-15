"""``python -m fallow_agent run`` — the agent daemon entrypoint.

A deliberately thin argparse wrapper: parse ``run --config <path.toml>``, build
the :class:`AgentRuntime` from the config file plus the process environment, and
run it until a shutdown signal. Configuration lives in the TOML file and
environment (see :mod:`fallow_agent.main.settings`), never in a pile of flags.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from fallow_agent.main.errors import AgentRuntimeError
from fallow_agent.main.runtime import AgentRuntime

_DEFAULT_CONFIG = Path("~/.fallow/agent.toml")
_EXIT_CONFIG_ERROR = 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fallow_agent", description="Fallow per-machine agent.")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Run the agent daemon.")
    run.add_argument(
        "--config",
        type=Path,
        default=_DEFAULT_CONFIG,
        help="Path to the agent TOML config file.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint. Returns a process exit code."""
    logging.basicConfig(level=logging.INFO)
    args = _build_parser().parse_args(argv)
    if args.command == "run":
        return _run(args.config)
    return _EXIT_CONFIG_ERROR


def _run(config_path: Path) -> int:
    try:
        runtime = AgentRuntime.from_config(config_path.expanduser(), dict(os.environ))
    except AgentRuntimeError as exc:
        print(f"fallow-agent: {exc}", file=sys.stderr)
        return _EXIT_CONFIG_ERROR
    asyncio.run(runtime.run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
