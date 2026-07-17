"""Warm-standby state export (ADR 054).

The export must produce a consistent, openable snapshot of the live WAL SQLite
state DB without locking or corrupting it, land atomically, and drive off an
injected clock. Promotion and agent re-pointing are out of scope for this
increment and are not exercised here.
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import suppress
from pathlib import Path

import aiosqlite

from fallow_coordinator.app.standby import export_snapshot, run_export_loop

# The pragmas the real registry/queue stores open the shared DB with.
_WAL_PRAGMAS = ("PRAGMA journal_mode=WAL", "PRAGMA synchronous=NORMAL")


async def _open_live_wal_db(db_path: Path) -> aiosqlite.Connection:
    """Open a WAL connection and seed a table, mirroring the live stores."""
    conn = await aiosqlite.connect(db_path)
    for pragma in _WAL_PRAGMAS:
        await conn.execute(pragma)
    await conn.execute("CREATE TABLE agents (id TEXT PRIMARY KEY)")
    await conn.commit()
    return conn


def _read_ids(snapshot: Path) -> list[str]:
    conn = sqlite3.connect(snapshot)
    try:
        rows = conn.execute("SELECT id FROM agents ORDER BY id").fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]


def _snapshot_agent_count(snapshot: Path) -> int:
    """Assert the snapshot passes integrity_check and return its agent row count."""
    conn = sqlite3.connect(snapshot)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        count = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
    finally:
        conn.close()
    return int(count)


async def test_snapshot_contains_committed_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "coordinator.db"
    conn = await _open_live_wal_db(db_path)
    try:
        await conn.execute("INSERT INTO agents (id) VALUES ('pc1'), ('pc2')")
        await conn.commit()

        snapshot = tmp_path / "standby" / "coordinator.db"
        await export_snapshot(db_path, snapshot)

        assert _read_ids(snapshot) == ["pc1", "pc2"]
    finally:
        await conn.close()


async def test_live_db_stays_usable_during_and_after_export(tmp_path: Path) -> None:
    db_path = tmp_path / "coordinator.db"
    conn = await _open_live_wal_db(db_path)
    try:
        await conn.execute("INSERT INTO agents (id) VALUES ('before')")
        await conn.commit()

        snapshot = tmp_path / "standby.db"
        await export_snapshot(db_path, snapshot)

        # The live connection is not locked out by the backup: it can still
        # write and read afterwards, and the write lands.
        await conn.execute("INSERT INTO agents (id) VALUES ('after')")
        await conn.commit()
        cursor = await conn.execute("SELECT COUNT(*) FROM agents")
        row = await cursor.fetchone()
        assert row is not None and row[0] == 2

        # The snapshot froze the committed state at export time.
        assert _read_ids(snapshot) == ["before"]
    finally:
        await conn.close()


async def test_live_db_writable_during_export(tmp_path: Path) -> None:
    """The live WAL DB stays writable *while* an export is in flight, not just after.

    Interleaves committed writes on the live connection with a running export and
    asserts every write lands, the live DB stays intact, and the snapshot is a
    consistent, openable copy.
    """
    db_path = tmp_path / "coordinator.db"
    conn = await _open_live_wal_db(db_path)
    try:
        # Seed enough pages that the backup copy spans several event-loop ticks,
        # so the writes below genuinely overlap it rather than racing a no-op.
        await conn.executemany(
            "INSERT INTO agents (id) VALUES (?)",
            [(f"seed-{i:05d}",) for i in range(3000)],
        )
        await conn.commit()

        snapshot = tmp_path / "standby.db"
        export = asyncio.create_task(export_snapshot(db_path, snapshot))

        live_writes = 0
        while not export.done():
            await conn.execute("INSERT INTO agents (id) VALUES (?)", (f"live-{live_writes:04d}",))
            await conn.commit()
            live_writes += 1
            await asyncio.sleep(0)
        await export

        # At least one commit landed while the export was still running.
        assert live_writes >= 1
        # Every interleaved write is durably readable on the live DB, which is
        # itself intact after the concurrent export.
        cursor = await conn.execute("SELECT COUNT(*) FROM agents WHERE id LIKE 'live-%'")
        row = await cursor.fetchone()
        assert row is not None and row[0] == live_writes
        cursor = await conn.execute("PRAGMA integrity_check")
        row = await cursor.fetchone()
        assert row is not None and row[0] == "ok"
        # The snapshot is a consistent copy holding at least the seeded rows.
        assert _snapshot_agent_count(snapshot) >= 3000
    finally:
        await conn.close()


async def test_export_is_atomic_and_leaves_no_partial(tmp_path: Path) -> None:
    db_path = tmp_path / "coordinator.db"
    conn = await _open_live_wal_db(db_path)
    try:
        await conn.execute("INSERT INTO agents (id) VALUES ('a')")
        await conn.commit()

        snapshot = tmp_path / "standby.db"
        await export_snapshot(db_path, snapshot)
        # A second export overwrites cleanly.
        await conn.execute("INSERT INTO agents (id) VALUES ('b')")
        await conn.commit()
        await export_snapshot(db_path, snapshot)

        assert _read_ids(snapshot) == ["a", "b"]
        assert not snapshot.with_name(f"{snapshot.name}.partial").exists()
    finally:
        await conn.close()


async def test_run_export_loop_writes_then_stops(tmp_path: Path) -> None:
    db_path = tmp_path / "coordinator.db"
    conn = await _open_live_wal_db(db_path)
    try:
        await conn.execute("INSERT INTO agents (id) VALUES ('x')")
        await conn.commit()

        snapshot = tmp_path / "standby.db"
        stop = asyncio.Event()

        async def fast_sleep(_seconds: float) -> None:
            await asyncio.sleep(0)

        task = asyncio.create_task(
            run_export_loop(
                source_db=db_path,
                dest=snapshot,
                interval_s=0.0,
                sleep=fast_sleep,
                stop_event=stop,
            )
        )
        for _ in range(200):
            if snapshot.exists():
                break
            await asyncio.sleep(0.005)
        stop.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

        assert _read_ids(snapshot) == ["x"]
    finally:
        await conn.close()
