"""Subprocess command runner for kill / net-drop events.

Used only by the ``__main__`` wiring — the injector takes a ``Runner`` callable,
so unit tests inject a recorder and never spawn a process. Commands come from
config templates (e.g. ``ssh {host} taskkill ...``); nothing is hardcoded here.
"""

from __future__ import annotations

import asyncio

from fallow_bench.churn.models import RunResult

_DETAIL_LIMIT = 500


async def run_shell(command: str) -> RunResult:
    """Execute ``command`` in a shell; ok iff it exits zero. Never raises."""
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
    except OSError as exc:  # spawn failure — treat as a failed disruption
        return RunResult(ok=False, detail=str(exc)[:_DETAIL_LIMIT])
    detail = stdout.decode("utf-8", errors="replace").strip()[:_DETAIL_LIMIT]
    return RunResult(ok=proc.returncode == 0, detail=detail)
