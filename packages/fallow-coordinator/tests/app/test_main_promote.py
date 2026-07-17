"""``python -m fallow_coordinator promote`` CLI glue (ADR 057).

Covers the argparse dispatch and source resolution around the promote core: the
snapshot source defaults to the config's ``standby_path``, ``--snapshot`` overrides
it, and an unset source is a clean startup error.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from fallow_coordinator.__main__ import main

_STATE_TABLES = ("registry_agents", "registry_models", "jobs", "work_units")


def _make_state_db(path: Path, agent: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        for table in _STATE_TABLES:
            conn.execute(f"CREATE TABLE {table} (id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO registry_agents (id) VALUES (?)", (agent,))
        conn.commit()
    finally:
        conn.close()


def _write_config(tmp_path: Path, *, standby_path: Path | None) -> Path:
    # Paths go into the TOML as POSIX (forward-slash) strings. A Windows tmp_path
    # carries backslashes, and a double-quoted TOML string would read "C:\Users"
    # as an escape ("\U...", an invalid hex escape). Forward slashes parse cleanly
    # and pathlib accepts them on every platform.
    lines = [
        f'db_path = "{(tmp_path / "live" / "coordinator.db").as_posix()}"',
        f'blob_dir = "{(tmp_path / "blobs").as_posix()}"',
        f'unit_input_dir = "{(tmp_path / "units").as_posix()}"',
        f'events_jsonl_path = "{(tmp_path / "events.jsonl").as_posix()}"',
        f'gateway_log_path = "{(tmp_path / "gateway.jsonl").as_posix()}"',
        'admin_key = "k"',
    ]
    if standby_path is not None:
        lines.append(f'standby_path = "{standby_path.as_posix()}"')
    config = tmp_path / "coordinator.toml"
    config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return config


def _agents(path: Path) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [row[0] for row in conn.execute("SELECT id FROM registry_agents")]
    finally:
        conn.close()


def test_promote_cli_installs_from_config_standby_path(tmp_path: Path) -> None:
    snapshot = tmp_path / "standby" / "coordinator.db"
    _make_state_db(snapshot, agent="from-standby")
    config = _write_config(tmp_path, standby_path=snapshot)

    main(["promote", "--config", str(config)])

    assert _agents(tmp_path / "live" / "coordinator.db") == ["from-standby"]


def test_promote_cli_snapshot_flag_overrides_config(tmp_path: Path) -> None:
    _make_state_db(tmp_path / "standby" / "coordinator.db", agent="from-standby")
    override = tmp_path / "elsewhere" / "coordinator.db"
    _make_state_db(override, agent="from-flag")
    config = _write_config(tmp_path, standby_path=tmp_path / "standby" / "coordinator.db")

    main(["promote", "--config", str(config), "--snapshot", str(override)])

    assert _agents(tmp_path / "live" / "coordinator.db") == ["from-flag"]


def test_promote_cli_errors_when_no_snapshot_source(tmp_path: Path) -> None:
    config = _write_config(tmp_path, standby_path=None)
    with pytest.raises(SystemExit, match="standby_path is unset"):
        main(["promote", "--config", str(config)])
