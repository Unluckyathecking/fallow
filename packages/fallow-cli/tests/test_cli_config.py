"""Unit tests for configuration resolution and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from fallow_cli.config import load_config, require_admin_key
from fallow_cli.errors import EXIT_AUTH, CliError


def _write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_precedence_flag_beats_env_and_file(tmp_path: Path) -> None:
    cfg_file = _write(tmp_path / "cli.toml", 'coordinator_url = "http://file"\n')
    env = {"FLW_COORDINATOR_URL": "http://env"}
    config = load_config("http://flag", env, config_path=cfg_file)
    assert config.coordinator_url == "http://flag"


def test_precedence_env_beats_file(tmp_path: Path) -> None:
    cfg_file = _write(tmp_path / "cli.toml", 'coordinator_url = "http://file"\n')
    config = load_config(None, {"FLW_COORDINATOR_URL": "http://env"}, config_path=cfg_file)
    assert config.coordinator_url == "http://env"


def test_precedence_file_used_when_no_flag_or_env(tmp_path: Path) -> None:
    cfg_file = _write(tmp_path / "cli.toml", 'coordinator_url = "http://file/"\n')
    config = load_config(None, {}, config_path=cfg_file)
    # trailing slash is normalised away
    assert config.coordinator_url == "http://file"


def test_admin_key_from_env_then_file(tmp_path: Path) -> None:
    cfg_file = _write(
        tmp_path / "cli.toml", 'coordinator_url = "http://c"\nadmin_key = "from-file"\n'
    )
    from_env = load_config(None, {"FLW_ADMIN_KEY": "from-env"}, config_path=cfg_file)
    assert from_env.admin_key == "from-env"
    from_file = load_config(None, {}, config_path=cfg_file)
    assert from_file.admin_key == "from-file"


def test_missing_url_raises_friendly_error(tmp_path: Path) -> None:
    with pytest.raises(CliError) as exc:
        load_config(None, {}, config_path=tmp_path / "absent.toml")
    assert "no coordinator URL configured" in exc.value.message


def test_invalid_url_scheme_rejected() -> None:
    with pytest.raises(CliError) as exc:
        load_config("ftp://c", {}, config_path=Path("/nonexistent"))
    assert "must start with http" in exc.value.message


def test_malformed_toml_reports_path(tmp_path: Path) -> None:
    cfg_file = _write(tmp_path / "cli.toml", "this is = = not toml")
    with pytest.raises(CliError) as exc:
        load_config(None, {}, config_path=cfg_file)
    assert "could not read config file" in exc.value.message


def test_unknown_config_key_rejected(tmp_path: Path) -> None:
    cfg_file = _write(tmp_path / "cli.toml", 'coordinator_url = "http://c"\nbogus = 1\n')
    with pytest.raises(CliError) as exc:
        load_config(None, {}, config_path=cfg_file)
    assert "unknown key" in exc.value.message


def test_require_admin_key_missing_raises_auth_exit(tmp_path: Path) -> None:
    config = load_config("http://c", {}, config_path=tmp_path / "absent.toml")
    with pytest.raises(CliError) as exc:
        require_admin_key(config)
    assert exc.value.exit_code == EXIT_AUTH
    assert "never pass it as a flag" in exc.value.message.lower()


def test_config_file_path_from_env(tmp_path: Path) -> None:
    cfg_file = _write(tmp_path / "custom.toml", 'coordinator_url = "http://c"\n')
    config = load_config(None, {"FLW_CONFIG_FILE": str(cfg_file)})
    assert config.coordinator_url == "http://c"
