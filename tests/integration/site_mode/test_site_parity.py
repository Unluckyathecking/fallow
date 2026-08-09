"""Legacy direct explicit-URL / Tailscale parity: Site Mode stays additive.

Proves the opt-in guarantee end to end: with Site Mode off, the coordinator mounts
no relay and a direct-mode Go daemon enrolls over an explicit ``coordinator_url``
(the same path a Tailscale deployment uses — an explicit URL and loopback/tailnet
bind) and is routed as a ``direct`` transport, unchanged by any Site Mode code.
"""

from __future__ import annotations

import json
from pathlib import Path

from site_mode.site_harness import (
    llama_command,
    mint_direct_token,
    run_site_daemon,
    serve_plain_coordinator,
    wait_for,
    write_direct_agent_toml,
)


async def test_direct_mode_parity_unchanged(site_binary: Path, tmp_path: Path) -> None:
    sub = tmp_path / "coord"
    sub.mkdir()
    async with serve_plain_coordinator(sub) as coord:
        # Site Mode is off by default: no relay broker, no site route resolver.
        assert coord.app.state.coordinator.relay is None
        assert coord.app.state.coordinator.site_route is None

        token = await mint_direct_token(coord)
        state = tmp_path / "direct-state.json"
        config = tmp_path / "direct-agent.toml"
        write_direct_agent_toml(
            config,
            coordinator_url=coord.base_url,  # explicit URL, as with Tailscale
            enrollment_token=token,
            state_path=state,
            cache_dir=tmp_path / "cache",
            llama_binary=llama_command(),
        )
        async with run_site_daemon(site_binary, config, state) as daemon:
            agent_id = await wait_for(
                lambda: _one_direct_agent(coord), timeout=20.0, what="direct enrollment"
            )
            assert agent_id, daemon.stderr
            # Routed as a direct transport, not through any relay.
            route = await coord.app.state.coordinator.registry.site_route(agent_id)
            assert route is not None and route[0].value == "direct"
            # The persisted identity is a plain direct identity (no site profile).
            identity = json.loads(state.read_text())
            assert identity.get("site") is None
            rc = await daemon.stop()
        assert rc == 0, daemon.stderr


async def _one_direct_agent(coord) -> str | None:
    resp = await coord.client.get("/v1/admin/agents", headers=coord.admin_headers())
    agents = resp.json() if resp.status_code == 200 else []
    return str(agents[0]["agent_id"]) if agents else None
