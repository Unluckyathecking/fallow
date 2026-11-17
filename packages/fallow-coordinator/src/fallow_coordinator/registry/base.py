"""Connection and clock plumbing shared by every part of :class:`SqliteRegistry`."""

import asyncio
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import aiosqlite

from fallow_coordinator.registry.config import RegistryConfig
from fallow_coordinator.registry.errors import RegistryNotOpenError
from fallow_coordinator.registry.tokens import new_token


class RegistryBase:
    """Owns the connection, config, clock, and write lock the domain mixins share."""

    def __init__(
        self,
        db_path: str | Path,
        config: RegistryConfig,
        now: Callable[[], datetime],
        token_factory: Callable[[], str] = new_token,
    ) -> None:
        self._db_path = str(db_path)
        self._config = config
        self._now = now
        self._new_token = token_factory
        self._db: aiosqlite.Connection | None = None
        # Serialises the writes that span more than one statement. aiosqlite runs
        # each statement on one worker thread, so no statement tears, but a
        # transaction is not a statement: every ``await`` between them is a point
        # where another coroutine runs on this same connection, inside this same
        # implicit transaction. ``register_agent`` rolls back there, and a
        # rollback discards whatever else is uncommitted — a revocation whose
        # ``UPDATE`` had landed but not yet committed was silently undone while
        # its route still answered 204.
        #
        # These four writers are the ones that must not interleave: the three
        # that consume or void a token, and ``set_assignments``, whose DELETE and
        # INSERTs are what revocation clears a desk's models with. The rest
        # commit a single statement, which can only commit a pending revocation
        # early, never unwind it; the rollback is what destroys work, and it
        # lives only in ``register_agent``.
        self._write_lock = asyncio.Lock()

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RegistryNotOpenError("registry connection is not open")
        return self._db

    def _iso_now(self) -> str:
        return self._now().isoformat()

    @staticmethod
    def _age_s(now: datetime, last_seen: str) -> float:
        return (now - datetime.fromisoformat(last_seen)).total_seconds()
