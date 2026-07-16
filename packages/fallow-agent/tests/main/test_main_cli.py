"""CLI: the reclaim/release subcommands toggle the flag file the daemon watches."""

from __future__ import annotations

from pathlib import Path

from fallow_agent.main.cli import main
from fallow_agent.preempt import reclaim_control_path

_TOML = """
coordinator_url = "http://coordinator.test/"
bind_host = "100.64.0.2"
llama_server_binary = "/usr/local/bin/llama-server"
"""


def _config(tmp_path: Path) -> tuple[Path, Path]:
    config = tmp_path / "agent.toml"
    state = tmp_path / "state" / "agent-state.json"
    config.write_text(_TOML + f'state_path = "{state.as_posix()}"\n', encoding="utf-8")
    return config, state


def test_reclaim_then_release_toggles_flag(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("FALLOW_STATE_PATH", raising=False)
    config, state = _config(tmp_path)
    flag = reclaim_control_path(state)

    assert main(["reclaim", "--config", str(config)]) == 0
    assert flag.exists()

    assert main(["release", "--config", str(config)]) == 0
    assert not flag.exists()


def test_control_reports_config_error(tmp_path: Path) -> None:
    missing = tmp_path / "nope.toml"
    assert main(["reclaim", "--config", str(missing)]) == 2
