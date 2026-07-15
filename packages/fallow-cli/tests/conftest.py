"""Fixtures for the fallow-cli tests.

Shared sample objects and MockTransport factories live in ``cli_helpers``.
Everything is deterministic and offline.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    """Base CLI env: admin key set, config file pointed at an empty tmp path."""
    return {"FLW_ADMIN_KEY": "secret", "FLW_CONFIG_FILE": str(tmp_path / "cli.toml")}
