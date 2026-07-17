"""Manual warm-standby promote (ADR 057).

Promote must validate a snapshot (opens, integrity_check, expected coordinator
tables) before installing it as the live ``db_path``, install atomically, clear
stale WAL sidecars, and refuse to overwrite a ``db_path`` newer than the snapshot
unless forced. A failed precondition must leave the live ``db_path`` untouched.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from fallow_coordinator.app.promote import PromoteError, promote, validate_snapshot
from fallow_coordinator.app.standby import export_snapshot

# The registry + queue tables promote treats as proof of a coordinator state DB.
_STATE_TABLES = ("registry_agents", "registry_models", "jobs", "work_units")


def _make_state_db(path: Path, agents: tuple[str, ...] = ("pc1", "pc2")) -> None:
    """Write a minimal but coordinator-shaped SQLite DB at ``path``."""
    conn = sqlite3.connect(path)
    try:
        for table in _STATE_TABLES:
            conn.execute(f"CREATE TABLE {table} (id TEXT PRIMARY KEY)")
        conn.executemany("INSERT INTO registry_agents (id) VALUES (?)", [(a,) for a in agents])
        conn.commit()
    finally:
        conn.close()


def _agent_ids(path: Path) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT id FROM registry_agents ORDER BY id").fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]


def _set_mtime(path: Path, epoch: float) -> None:
    os.utime(path, (epoch, epoch))


# ── validate_snapshot ────────────────────────────────────────────────────────
def test_validate_accepts_a_coordinator_state_db(tmp_path: Path) -> None:
    snapshot = tmp_path / "snap.db"
    _make_state_db(snapshot)
    validate_snapshot(snapshot)  # does not raise


def test_validate_rejects_a_missing_snapshot(tmp_path: Path) -> None:
    with pytest.raises(PromoteError, match="not found"):
        validate_snapshot(tmp_path / "absent.db")


def test_validate_rejects_an_empty_snapshot(tmp_path: Path) -> None:
    snapshot = tmp_path / "empty.db"
    snapshot.touch()
    with pytest.raises(PromoteError, match="empty"):
        validate_snapshot(snapshot)


def test_validate_rejects_a_non_database_file(tmp_path: Path) -> None:
    snapshot = tmp_path / "garbage.db"
    snapshot.write_bytes(b"this is not a sqlite database" * 8)
    with pytest.raises(PromoteError, match="not a valid SQLite database"):
        validate_snapshot(snapshot)


def test_validate_rejects_a_db_without_the_expected_tables(tmp_path: Path) -> None:
    # Shaped like the sibling rag.db: a real SQLite DB, but none of the state tables.
    snapshot = tmp_path / "rag.db"
    conn = sqlite3.connect(snapshot)
    try:
        conn.execute("CREATE TABLE rag_collections (id TEXT PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(PromoteError, match="missing tables"):
        validate_snapshot(snapshot)


# ── promote ──────────────────────────────────────────────────────────────────
def test_promote_installs_snapshot_as_db_path(tmp_path: Path) -> None:
    snapshot = tmp_path / "standby.db"
    _make_state_db(snapshot, agents=("a", "b", "c"))
    db_path = tmp_path / "live" / "coordinator.db"  # parent does not exist yet

    promote(snapshot, db_path)

    assert db_path.is_file()
    assert _agent_ids(db_path) == ["a", "b", "c"]


def test_promote_refuses_to_clobber_a_newer_db_path(tmp_path: Path) -> None:
    snapshot = tmp_path / "standby.db"
    _make_state_db(snapshot, agents=("snap",))
    _set_mtime(snapshot, 1000.0)

    db_path = tmp_path / "coordinator.db"
    _make_state_db(db_path, agents=("live",))
    _set_mtime(db_path, 2000.0)  # newer than the snapshot → still-running/newer primary

    with pytest.raises(PromoteError, match="newer than the snapshot"):
        promote(snapshot, db_path)

    # The live DB is left exactly as it was.
    assert _agent_ids(db_path) == ["live"]


def test_promote_force_overwrites_a_newer_db_path(tmp_path: Path) -> None:
    snapshot = tmp_path / "standby.db"
    _make_state_db(snapshot, agents=("snap",))
    _set_mtime(snapshot, 1000.0)

    db_path = tmp_path / "coordinator.db"
    _make_state_db(db_path, agents=("live",))
    _set_mtime(db_path, 2000.0)

    promote(snapshot, db_path, force=True)

    assert _agent_ids(db_path) == ["snap"]


def test_promote_replaces_an_older_db_path_without_force(tmp_path: Path) -> None:
    snapshot = tmp_path / "standby.db"
    _make_state_db(snapshot, agents=("snap",))
    _set_mtime(snapshot, 2000.0)

    db_path = tmp_path / "coordinator.db"
    _make_state_db(db_path, agents=("stale",))
    _set_mtime(db_path, 1000.0)  # older than the snapshot → safe to replace

    promote(snapshot, db_path)

    assert _agent_ids(db_path) == ["snap"]


def test_promote_clears_stale_wal_sidecars(tmp_path: Path) -> None:
    snapshot = tmp_path / "standby.db"
    _make_state_db(snapshot, agents=("snap",))
    _set_mtime(snapshot, 2000.0)

    db_path = tmp_path / "coordinator.db"
    _make_state_db(db_path, agents=("stale",))
    _set_mtime(db_path, 1000.0)
    wal = db_path.with_name(f"{db_path.name}-wal")
    shm = db_path.with_name(f"{db_path.name}-shm")
    wal.write_bytes(b"stale-wal")
    shm.write_bytes(b"stale-shm")

    promote(snapshot, db_path)

    # The stale sidecars from the previous db_path must not survive to be replayed.
    assert not wal.exists()
    assert not shm.exists()
    assert _agent_ids(db_path) == ["snap"]


def test_promote_leaves_db_path_untouched_on_invalid_snapshot(tmp_path: Path) -> None:
    snapshot = tmp_path / "garbage.db"
    snapshot.write_bytes(b"not a database")

    db_path = tmp_path / "coordinator.db"
    _make_state_db(db_path, agents=("live",))

    with pytest.raises(PromoteError):
        promote(snapshot, db_path)

    assert _agent_ids(db_path) == ["live"]


async def test_promote_accepts_a_real_exporter_snapshot(tmp_path: Path) -> None:
    """The snapshot the ADR 054 exporter produces is one promote accepts and installs."""
    source = tmp_path / "coordinator.db"
    _make_state_db(source, agents=("x", "y"))

    snapshot = tmp_path / "standby" / "coordinator.db"
    await export_snapshot(source, snapshot)

    db_path = tmp_path / "promoted" / "coordinator.db"
    promote(snapshot, db_path)

    assert _agent_ids(db_path) == ["x", "y"]
