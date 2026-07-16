"""Manual promote of a warm-standby snapshot to live coordinator state (ADR 057).

ADR 054 ships the export half of the warm standby: a live coordinator writes a
consistent snapshot of its state DB (``db_path``: registry + queue) to
``standby_path``. This module is the other half — the operator-run promote that
installs such a snapshot as a coordinator's live ``db_path`` so a fresh process
resumes from the last exported state.

Promotion is deliberately manual (ADR 057): there is no automatic detection or
election. On coordinator loss an operator runs ``promote`` on the standby host,
then starts the coordinator there. Before installing anything this validates the
snapshot (it opens, passes ``integrity_check``, and carries the expected
coordinator tables) and refuses to overwrite a ``db_path`` newer than the snapshot
unless forced, so a mistaken promote cannot clobber a still-running or
more-recent primary.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

# A valid snapshot must contain all of these. They are the load-bearing tables of
# the registry and the queue — the two stores that share ``db_path``. The set
# rejects an empty or corrupt file and the sibling ``rag.db`` (whose tables are
# entirely disjoint), without coupling to the full evolving schema.
_REQUIRED_TABLES = ("registry_agents", "registry_models", "jobs", "work_units")


class PromoteError(RuntimeError):
    """A promote precondition failed; the live ``db_path`` was left untouched."""


def validate_snapshot(snapshot: Path) -> None:
    """Raise :class:`PromoteError` unless ``snapshot`` is an intact state DB.

    Checks, in order: the file exists and is non-empty; it opens and passes
    SQLite's ``integrity_check``; and it holds every table in
    :data:`_REQUIRED_TABLES`.
    """
    if not snapshot.is_file():
        raise PromoteError(f"snapshot not found: {snapshot}")
    if snapshot.stat().st_size == 0:
        raise PromoteError(f"snapshot is empty: {snapshot}")
    try:
        conn = sqlite3.connect(snapshot)
    except sqlite3.Error as exc:  # pragma: no cover - connect is lazy, rarely raises here
        raise PromoteError(f"cannot open snapshot {snapshot}: {exc}") from exc
    try:
        _check_integrity(conn, snapshot)
        present = _table_names(conn)
    except sqlite3.DatabaseError as exc:
        raise PromoteError(f"snapshot is not a valid SQLite database: {snapshot}") from exc
    finally:
        conn.close()
    missing = [name for name in _REQUIRED_TABLES if name not in present]
    if missing:
        raise PromoteError(
            f"snapshot is not a coordinator state DB "
            f"(missing tables: {', '.join(missing)}): {snapshot}"
        )


def promote(snapshot: Path, db_path: Path, *, force: bool = False) -> None:
    """Install a validated ``snapshot`` as the coordinator's live ``db_path``.

    Validates the snapshot, refuses to overwrite a ``db_path`` at least as new as
    the snapshot unless ``force`` is set, then swaps it in atomically and clears
    any stale WAL sidecars so a fresh coordinator resumes cleanly.
    """
    validate_snapshot(snapshot)
    if not force and _would_clobber_newer(snapshot, db_path):
        raise PromoteError(
            f"db_path is newer than the snapshot and would be overwritten: {db_path}. "
            "The primary may still be running; stop it, then re-run with --force."
        )
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = db_path.with_name(f"{db_path.name}.promote")
    tmp.unlink(missing_ok=True)
    shutil.copyfile(snapshot, tmp)
    os.replace(tmp, db_path)
    # A stale WAL/SHM beside the previous db_path must not be replayed onto the
    # freshly installed main file. SQLite recreates them cleanly on next open.
    for sidecar in (f"{db_path.name}-wal", f"{db_path.name}-shm"):
        db_path.with_name(sidecar).unlink(missing_ok=True)


def _check_integrity(conn: sqlite3.Connection, snapshot: Path) -> None:
    row = conn.execute("PRAGMA integrity_check").fetchone()
    if row is None or row[0] != "ok":
        detail = row[0] if row is not None else "no result"
        raise PromoteError(f"snapshot failed integrity_check ({detail}): {snapshot}")


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {str(name) for (name,) in rows}


def _would_clobber_newer(snapshot: Path, db_path: Path) -> bool:
    """True when ``db_path`` exists and is at least as recent as ``snapshot``.

    A live coordinator writes ``db_path`` continuously, so a ``db_path`` at least
    as new as the periodic snapshot is the signal for a still-running or
    more-recent primary — promoting would discard state the snapshot never held.
    An absent or older ``db_path`` is safe to replace.
    """
    if not db_path.exists():
        return False
    return db_path.stat().st_mtime >= snapshot.stat().st_mtime


__all__ = ["PromoteError", "promote", "validate_snapshot"]
