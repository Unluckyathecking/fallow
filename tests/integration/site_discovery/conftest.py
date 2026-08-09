"""Fixtures for the LAN Site Mode discovery acceptance suite.

Like the static suite next door, every scenario drives the real Go Site runtime,
so the built binary is required and a missing one fails loudly: a skipped
acceptance lane is a failed acceptance run.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from site_mode.site_harness import go_agent_binary


@pytest.fixture(scope="session")
def site_binary() -> Path:
    return go_agent_binary()
