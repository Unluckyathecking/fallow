"""Settings resolution, environment overrides, and replica bind safety."""

from __future__ import annotations

from pathlib import Path

import pytest

from fallow_agent.main import SettingsError, load_settings

_TOML = """
coordinator_url = "http://coordinator.test/"
bind_host = "100.64.0.2"
llama_server_binary = "/usr/local/bin/llama-server"
enrollment_token = "file-token"
[port_range]
start = 8100
count = 8
[whisper]
model_size_or_path = "base"
"""


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "agent.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_loads_file_values(tmp_path: Path) -> None:
    settings = load_settings(_write(tmp_path, _TOML), env={})
    assert settings.coordinator_url == "http://coordinator.test"  # trailing slash stripped
    assert settings.bind_host == "100.64.0.2"
    assert settings.enrollment_token == "file-token"
    assert settings.port_range.start == 8100
    assert settings.whisper.model_size_or_path == "base"


def test_env_overrides_file(tmp_path: Path) -> None:
    env = {
        "FALLOW_COORDINATOR_URL": "http://other.test",
        "FALLOW_ENROLLMENT_TOKEN": "env-token",
        "FALLOW_PORT_START": "9000",
    }
    settings = load_settings(_write(tmp_path, _TOML), env=env)
    assert settings.coordinator_url == "http://other.test"
    assert settings.enrollment_token == "env-token"  # env wins over file
    assert settings.port_range.start == 9000
    assert settings.port_range.count == 8  # untouched file value


@pytest.mark.parametrize(
    "bind_host",
    [
        "",
        "   ",
        "0",
        "0.0",
        "0.0.0",
        "0.0.0.0",
        "::",
        "0::0",
        "[::]",
        "::ffff:0.0.0.0",
        "::ffff:0:0",
        "*",
    ],
)
def test_rejects_bind_host_all_interfaces(tmp_path: Path, bind_host: str) -> None:
    body = _TOML.replace('bind_host = "100.64.0.2"', f'bind_host = "{bind_host}"')
    with pytest.raises(
        SettingsError,
        match="expose the unauthenticated llama-server",
    ):
        load_settings(_write(tmp_path, body), env={})


def test_env_can_inject_forbidden_bind_host_and_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="expose the unauthenticated llama-server"):
        load_settings(_write(tmp_path, _TOML), env={"FALLOW_BIND_HOST": "0.0.0.0"})


@pytest.mark.parametrize("bind_host", ["100.64.0.2", "127.0.0.1", "::ffff:127.0.0.1"])
def test_accepts_tailnet_and_loopback_bind_hosts(tmp_path: Path, bind_host: str) -> None:
    body = _TOML.replace('bind_host = "100.64.0.2"', f'bind_host = "{bind_host}"')
    assert load_settings(_write(tmp_path, body), env={}).bind_host == bind_host


def test_missing_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(SettingsError, match="not found"):
        load_settings(tmp_path / "nope.toml", env={})


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    body = 'bogus = "x"\n' + _TOML  # top-level, before any table
    with pytest.raises(SettingsError, match="unknown key"):
        load_settings(_write(tmp_path, body), env={})
