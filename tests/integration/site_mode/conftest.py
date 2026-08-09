"""Fixtures for the static LAN Site Mode acceptance suite.

The whole directory drives the *real* Go Site runtime against a pinned-HTTPS
coordinator, so it needs the built binary. A missing binary fails loudly (see
``site_binary``) because a skipped acceptance lane is a failed acceptance run.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from site_harness import (
    SiteCoordinator,
    go_agent_binary,
    serve_site_coordinator,
)


@pytest.fixture(scope="session")
def site_binary() -> Path:
    return go_agent_binary()


@pytest_asyncio.fixture
async def coordinator(tmp_path: Path) -> AsyncIterator[SiteCoordinator]:
    sub = tmp_path / "coord"
    sub.mkdir()
    async with serve_site_coordinator(sub) as coord:
        yield coord
