"""``AgentRuntime``: the top-level object the CLI drives.

It owns the run loop: build the agent (register-or-load, wire everything), start
every component, then block until a shutdown signal (SIGINT/SIGTERM) or a fatal
condition (an auth rejection surfaced by the heartbeat loop) fires. On shutdown
it drives the graceful teardown in :class:`AgentServices` and releases the HTTP
client. Nothing here touches module internals — it composes only public APIs.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Mapping
from pathlib import Path

from fallow_agent.main.assembly import AgentAssembly
from fallow_agent.main.seams import RuntimeSeams
from fallow_agent.main.settings import AgentSettings, load_settings

logger = logging.getLogger(__name__)

_SHUTDOWN_SIGNALS = (signal.SIGINT, signal.SIGTERM)


class AgentRuntime:
    """Build, run, and gracefully stop the per-machine Fallow agent."""

    def __init__(self, settings: AgentSettings, *, seams: RuntimeSeams | None = None) -> None:
        self._settings = settings
        self._seams = seams or RuntimeSeams()

    @classmethod
    def from_config(cls, config_path: Path, env: Mapping[str, str]) -> AgentRuntime:
        """Construct a runtime from a TOML config path and an environment map."""
        return cls(load_settings(config_path, env))

    async def run(self) -> None:
        """Run until a shutdown signal or a fatal auth rejection, then stop."""
        shutdown = asyncio.Event()
        assembly = AgentAssembly(self._settings, self._seams, on_fatal=shutdown.set)
        built = await assembly.build()
        built.services.start()
        self._install_signal_handlers(shutdown)
        logger.info("fallow agent started")
        try:
            await shutdown.wait()
        finally:
            self._remove_signal_handlers()
            logger.info("fallow agent shutting down")
            await built.services.stop()
            await built.aclose()

    # ── Signals ──────────────────────────────────────────────────────────────

    def _install_signal_handlers(self, shutdown: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()
        for sig in _SHUTDOWN_SIGNALS:
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, shutdown.set)

    def _remove_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in _SHUTDOWN_SIGNALS:
            with contextlib.suppress(NotImplementedError, ValueError):
                loop.remove_signal_handler(sig)
