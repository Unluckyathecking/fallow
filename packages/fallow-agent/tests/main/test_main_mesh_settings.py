"""The [mesh] settings table: off by default, key required when enabled."""

from __future__ import annotations

from pathlib import Path

import pytest

from fallow_agent.main import SettingsError, load_settings

_BASE = """
coordinator_url = "http://coordinator.test/"
bind_host = "100.64.0.2"
llama_server_binary = "/usr/local/bin/llama-server"
"""


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "agent.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_mesh_off_by_default(tmp_path: Path) -> None:
    settings = load_settings(_write(tmp_path, _BASE), env={})
    assert settings.mesh.enabled is False
    assert settings.mesh.signing_key is None


def test_mesh_table_parsed(tmp_path: Path) -> None:
    body = _BASE + '\n[mesh]\nenabled = true\nsigning_key = "shared"\nstore_capacity_bytes = 4096\n'
    settings = load_settings(_write(tmp_path, body), env={})
    assert settings.mesh.enabled is True
    assert settings.mesh.signing_key == "shared"
    assert settings.mesh.store_capacity_bytes == 4096


def test_mesh_enabled_without_key_is_rejected(tmp_path: Path) -> None:
    body = _BASE + "\n[mesh]\nenabled = true\n"
    with pytest.raises(SettingsError, match=r"signing_key"):
        load_settings(_write(tmp_path, body), env={})
