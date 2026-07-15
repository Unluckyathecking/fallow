"""The [bench] settings table: off by default, parseable, default port 9411."""

from __future__ import annotations

from pathlib import Path

import pytest

from fallow_agent.main import SettingsError, load_settings
from fallow_agent.main.settings import DEFAULT_BENCH_PORT

_BASE = """
coordinator_url = "http://coordinator.test/"
bind_host = "100.64.0.2"
llama_server_binary = "/usr/local/bin/llama-server"
"""


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "agent.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_bench_off_by_default(tmp_path: Path) -> None:
    settings = load_settings(_write(tmp_path, _BASE), env={})
    assert settings.bench.enabled is False
    assert settings.bench.force_idle is False
    assert settings.bench.port == DEFAULT_BENCH_PORT == 9411


def test_bench_table_parsed(tmp_path: Path) -> None:
    body = _BASE + "\n[bench]\nenabled = true\nforce_idle = true\nport = 9500\n"
    settings = load_settings(_write(tmp_path, body), env={})
    assert settings.bench.enabled is True
    assert settings.bench.force_idle is True
    assert settings.bench.port == 9500


def test_bench_enabled_without_explicit_port_uses_default(tmp_path: Path) -> None:
    body = _BASE + "\n[bench]\nenabled = true\n"
    settings = load_settings(_write(tmp_path, body), env={})
    assert settings.bench.enabled is True
    assert settings.bench.port == DEFAULT_BENCH_PORT


def test_bench_rejects_non_positive_port(tmp_path: Path) -> None:
    body = _BASE + "\n[bench]\nenabled = true\nport = 0\n"
    with pytest.raises(SettingsError):
        load_settings(_write(tmp_path, body), env={})


def test_force_idle_rejected_when_bench_is_disabled(tmp_path: Path) -> None:
    body = _BASE + "\n[bench]\nforce_idle = true\n"
    with pytest.raises(SettingsError, match=r"force_idle requires bench\.enabled"):
        load_settings(_write(tmp_path, body), env={})


def test_settings_are_frozen(tmp_path: Path) -> None:
    settings = load_settings(_write(tmp_path, _BASE), env={})
    with pytest.raises(Exception):  # noqa: B017 - pydantic frozen raises ValidationError
        settings.bench.enabled = True  # type: ignore[misc]
