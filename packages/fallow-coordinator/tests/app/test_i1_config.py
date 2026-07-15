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
    assert config.max_result_payload_bytes == 64 * 1024 * 1024
    # Defaults fill in the rest.
    assert config.long_poll_max_s == 25.0


def test_env_overrides_win(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FALLOW_COORD_ADMIN_KEY", "from-env")
    monkeypatch.setenv("FALLOW_COORD_PORT", "7777")
    config = load_config(_write_toml(tmp_path))
    assert config.admin_key == "from-env"
    assert config.port == 7777


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
