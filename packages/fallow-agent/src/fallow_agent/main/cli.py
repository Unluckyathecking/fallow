"""``python -m fallow_agent`` — the agent daemon entrypoint and control CLI.

A deliberately thin argparse wrapper over three subcommands: ``run`` builds the
:class:`AgentRuntime` and runs it until a shutdown signal; ``reclaim`` and
``release`` are the user's instant takedown control — they write and remove a
flag file the running daemon's poll loop watches (see ADR 042). Configuration
lives in the TOML file and environment (see :mod:`fallow_agent.main.settings`),
never in a pile of flags.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from fallow_agent.main.errors import AgentRuntimeError, SettingsError
from fallow_agent.main.runtime import AgentRuntime
from fallow_agent.main.settings import load_settings
from fallow_agent.preempt import request_reclaim, request_release

_DEFAULT_CONFIG = Path("~/.fallow/agent.toml")
_EXIT_CONFIG_ERROR = 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fallow_agent", description="Fallow per-machine agent.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("run", "Run the agent daemon."),
        ("reclaim", "Reclaim this machine now: stop all local serving until release."),
        ("release", "Release the machine: restore normal idle-based serving."),
    ):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument(
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
    if args.command == "reclaim":
        return _control(args.config, reclaim=True)
    if args.command == "release":
        return _control(args.config, reclaim=False)
    return _EXIT_CONFIG_ERROR


def _run(config_path: Path) -> int:
    try:
        runtime = AgentRuntime.from_config(config_path.expanduser(), dict(os.environ))
    except AgentRuntimeError as exc:
        print(f"fallow-agent: {exc}", file=sys.stderr)
        return _EXIT_CONFIG_ERROR
    asyncio.run(runtime.run())
    return 0


def _control(config_path: Path, *, reclaim: bool) -> int:
    """Write or remove the reclaim flag the running daemon watches."""
    try:
        settings = load_settings(config_path.expanduser(), dict(os.environ))
    except SettingsError as exc:
        print(f"fallow-agent: {exc}", file=sys.stderr)
        return _EXIT_CONFIG_ERROR
    if reclaim:
        path = request_reclaim(settings.state_path)
        print(f"reclaim requested; the daemon will stop serving (flag: {path})")
    else:
        path = request_release(settings.state_path)
        print(f"release requested; normal serving will resume (flag: {path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
