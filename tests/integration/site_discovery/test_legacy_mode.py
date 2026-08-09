"""A legacy direct agent never starts discovery.

ADR 092's compatibility clause. Site Mode is opt-in and discovery is opt-in
inside it, so an agent configured the old way — an explicit coordinator URL and
an enrollment token — has to behave exactly as it did before: it persists no
site profile, probes nothing and queries nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from site_mode.site_harness import (
    llama_command,
    mint_direct_token,
    run_site_daemon,
    serve_plain_coordinator,
    wait_enrolled,
    write_direct_agent_toml,
)

from site_discovery.discovery_harness import (
    NO_CANDIDATE,
    ORIGIN_UNUSABLE,
    QUERY_OPENED,
)

pytestmark = pytest.mark.asyncio


async def test_legacy_direct_mode_starts_no_discovery(site_binary: Path, tmp_path: Path) -> None:
    coord_dir = tmp_path / "coord"
    coord_dir.mkdir()
    state = tmp_path / "agent-state.json"
    config = tmp_path / "agent.toml"

    async with serve_plain_coordinator(coord_dir) as coord:
        token = await mint_direct_token(coord)
        write_direct_agent_toml(
            config,
            coordinator_url=coord.base_url,
            enrollment_token=token,
            state_path=state,
            cache_dir=tmp_path / "cache",
            llama_binary=llama_command(),
        )
        async with run_site_daemon(site_binary, config, state) as daemon:
            agent_id = await wait_enrolled(coord)
            rc = await daemon.stop()
        assert rc == 0, daemon.stderr

    identity = json.loads(state.read_text(encoding="utf-8"))
    assert identity["agent_id"] == agent_id
    assert "site" not in identity, "a direct agent must not persist a site profile"

    log = daemon.stderr
    assert QUERY_OPENED not in log, log
    assert NO_CANDIDATE not in log, log
    assert ORIGIN_UNUSABLE not in log, log
