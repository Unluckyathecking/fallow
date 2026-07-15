"""CoordinatorConfig loading: TOML parse + FALLOW_COORD_* env overrides."""

from __future__ import annotations

from pathlib import Path

import pytest

from fallow_coordinator.app import create_app, load_config
from fallow_coordinator.app.config import CoordinatorConfig

_TOML = """
db_path = "/data/coordinator.db"
blob_dir = "/data/blobs"
unit_input_dir = "/data/units"
result_dir = "/data/results"
events_jsonl_path = "/data/events.jsonl"
gateway_log_path = "/data/gateway.jsonl"
admin_key = "from-file"
port = 9000
chunks_per_unit = 16
"""


def _write_toml(tmp_path: Path) -> Path:
    path = tmp_path / "coordinator.toml"
    path.write_text(_TOML, encoding="utf-8")
    return path


def test_load_config_from_toml(tmp_path: Path) -> None:
    config = load_config(_write_toml(tmp_path))
    assert config.admin_key == "from-file"
    assert config.port == 9000
    assert config.chunks_per_unit == 16
    assert config.db_path == Path("/data/coordinator.db")
    assert config.result_dir == Path("/data/results")
    assert config.churn_history_jsonl_path == Path("/data/events.jsonl")
    assert config.max_result_payload_bytes == 64 * 1024 * 1024
    assert config.affinity_ttl_s == 1800.0
    assert config.affinity_max == 10_000
    # Defaults fill in the rest.
    assert config.long_poll_max_s == 25.0
    assert config.quota_snapshot_interval_s == 30.0
    assert config.admission_timeout_s == 10.0
    assert config.admission_capacity == 64


def test_old_config_derives_result_dir_beside_database(tmp_path: Path) -> None:
    path = tmp_path / "coordinator.toml"
    path.write_text(_TOML.replace('result_dir = "/data/results"\n', ""), encoding="utf-8")

    config = load_config(path)

    assert config.result_dir == Path("/data/results")


def test_config_accepts_separate_churn_history(tmp_path: Path) -> None:
    path = _write_toml(tmp_path)
    path.write_text(
        _TOML + 'churn_history_jsonl_path = "/data/history.jsonl"\n',
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.events_jsonl_path == Path("/data/events.jsonl")
    assert config.churn_history_jsonl_path == Path("/data/history.jsonl")


def test_env_overrides_win(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FALLOW_COORD_ADMIN_KEY", "from-env")
    monkeypatch.setenv("FALLOW_COORD_PORT", "7777")
    monkeypatch.setenv("FALLOW_COORD_ADMISSION_TIMEOUT_S", "4.5")
    monkeypatch.setenv("FALLOW_COORD_ADMISSION_CAPACITY", "12")
    monkeypatch.setenv("FALLOW_COORD_AFFINITY_TTL_S", "90")
    monkeypatch.setenv("FALLOW_COORD_AFFINITY_MAX", "25")
    config = load_config(_write_toml(tmp_path))
    assert config.admin_key == "from-env"
    assert config.port == 7777
    assert config.admission_timeout_s == 4.5
    assert config.admission_capacity == 12
    assert config.affinity_ttl_s == 90.0
    assert config.affinity_max == 25


def test_config_is_frozen() -> None:
    config = CoordinatorConfig(
        db_path=Path("/d/c.db"),
        blob_dir=Path("/d/b"),
        unit_input_dir=Path("/d/u"),
        result_dir=Path("/d/r"),
        events_jsonl_path=Path("/d/e.jsonl"),
        gateway_log_path=Path("/d/g.jsonl"),
        admin_key="k",
    )
    with pytest.raises((ValueError, TypeError)):
        config.port = 1234  # type: ignore[misc]


async def test_create_app_creates_result_directory(tmp_path: Path) -> None:
    result_dir = tmp_path / "nested" / "results"
    config = CoordinatorConfig(
        db_path=tmp_path / "coordinator.db",
        blob_dir=tmp_path / "blobs",
        unit_input_dir=tmp_path / "units",
        result_dir=result_dir,
        events_jsonl_path=tmp_path / "events.jsonl",
        gateway_log_path=tmp_path / "gateway.jsonl",
        admin_key="k",
    )

    app = create_app(config)

    assert result_dir.is_dir()
    await app.state.coordinator.client.aclose()
