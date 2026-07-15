"""CLI parsing and env-var resolution for the workload entry point."""

from __future__ import annotations

from pathlib import Path

import pytest

from fallow_bench.workload import cli


def test_parser_accepts_run_command() -> None:
    args = cli.build_parser().parse_args(["run", "--config", "a.yaml", "--out", "runs/x"])
    assert args.command == "run"
    assert args.config == Path("a.yaml")
    assert args.out == Path("runs/x")


def test_parser_requires_subcommand() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])


def test_require_env_returns_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLW_TEST_KEY", "secret")
    assert cli._require_env("FLW_TEST_KEY") == "secret"


def test_require_env_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FLW_TEST_KEY", raising=False)
    with pytest.raises(SystemExit, match="FLW_TEST_KEY"):
        cli._require_env("FLW_TEST_KEY")
