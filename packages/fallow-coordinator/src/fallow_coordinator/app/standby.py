"""Warm-standby state export for the coordinator (module I1, ADR 054).

The coordinator's durable state is a single WAL-mode SQLite file (``db_path``,
holding the registry and the queue). This module ships a consistent copy of that
file to a configured standby location so a dead coordinator no longer takes the
whole fabric down.

The snapshot is produced with the SQLite online backup API against a *separate*
connection to the live file. That API is designed for exactly this: it reads a
transactionally consistent view of a database that other connections are still
writing, so the export never locks or corrupts the live DB. A naive file copy of
a WAL database would not be safe — the committed pages can live in the ``-wal``
sidecar, not the main file. The copy is written to a temp file and atomically
renamed into place, so a crash mid-export can never leave a partial snapshot at
the destination.

Promotion of the standby and re-pointing agents are deliberately out of scope for
this increment; see ADR 054.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path

logger = logging.getLogger(__name__)

Sleeper = Callable[[float], Awaitable[None]]


def _backup_and_replace(source_db: Path, dest: Path, tmp: Path) -> None:
    """Copy ``source_db`` to ``tmp`` via the backup API, then atomically rename.

    Runs in a worker thread: ``sqlite3`` is blocking, and the live DB is served
    from its own connections on the event loop, so the two never contend beyond
    SQLite's own cross-connection WAL locking.
    """
    # A crashed earlier export can leave an invalid ``tmp`` behind. Opening it and
    # backing up into it would then fail with "file is not a database" on every
    # future run, silently staling the standby. Start each export from a clean tmp.
    tmp.unlink(missing_ok=True)
    source = sqlite3.connect(source_db)
    try:
        target = sqlite3.connect(tmp)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    os.replace(tmp, dest)


async def export_snapshot(source_db: Path, dest: Path) -> None:
    """Write a consistent snapshot of ``source_db`` to ``dest``."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f"{dest.name}.partial")
    await asyncio.to_thread(_backup_and_replace, source_db, dest, tmp)


async def run_export_loop(
    *,
    source_db: Path,
    dest: Path,
    interval_s: float,
    sleep: Sleeper,
    stop_event: asyncio.Event,
) -> None:
    """Export a snapshot every ``interval_s`` until ``stop_event`` is set.

    A failed export is logged and the loop continues: a transient I/O error at
    the standby location must never take the live coordinator down.
    """
    while not stop_event.is_set():
        await sleep(interval_s)
        if stop_event.is_set():
            return
        try:
            await export_snapshot(source_db, dest)
        except Exception:  # a bad export must never kill the loop
            logger.exception("coordinator state export failed")
