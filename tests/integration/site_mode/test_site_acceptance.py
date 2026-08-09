"""The static Site Mode happy path: prove the full outbound-only vertical.

One test walks the whole contract end to end through the real components — a
pinned-HTTPS coordinator on an exact loopback address, a join file minted through
``flw``, the built Go Site runtime enrolled once against a token-free profile, and
a loopback-only fake llama the supervisor spawns — then routes a buffered JSON
request and a raw SSE request through the outbound claim relay and checks the
status, content type and body survive byte for byte.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from site_harness import (
    CHAT_MODEL,
    SiteCoordinator,
    assign_model,
    create_api_key,
    llama_command,
    mint_join_bundle_via_flw,
    register_chat_model,
    run_site_daemon,
    wait_enrolled,
    wait_replica_ready,
    write_agent_toml,
)

pytestmark = pytest.mark.asyncio


async def _serving_agent(coord: SiteCoordinator, tmp_path: Path, binary: Path):
    """Bring one Site Mode agent to READY and return (daemon_cm, agent_id, key)."""
    blob = tmp_path / "model.gguf"
    blob.write_bytes(b"fake-gguf-bytes-for-the-pilot")
    await register_chat_model(coord, blob)
    key = await create_api_key(coord)

    join = await asyncio.to_thread(mint_join_bundle_via_flw, coord, tmp_path / "join")
    state = tmp_path / "agent-state.json"
    config = tmp_path / "agent.toml"
    write_agent_toml(
        config,
        join_bundle=join,
        state_path=state,
        cache_dir=tmp_path / "cache",
        llama_binary=llama_command(),
    )
    return blob, key, join, state, config


async def test_static_site_vertical_buffered_and_sse(
    coordinator: SiteCoordinator, site_binary: Path, tmp_path: Path
) -> None:
    coord = coordinator
    blob, key, join, state, config = await _serving_agent(coord, tmp_path, site_binary)

    async with run_site_daemon(site_binary, config, state) as daemon:
        agent_id = await wait_enrolled(coord)

        # Enrolled once and persisted a token-free identity; the join file is gone.
        identity = daemon.identity()
        assert identity["agent_id"] == agent_id
        assert identity.get("site") is not None
        assert "enrollment_token" not in json.dumps(identity)
        assert not join.exists(), "join file must be removed after enrollment"

        await assign_model(coord, [agent_id])
        port = await wait_replica_ready(coord, agent_id)
        assert 8100 <= port < 8108, f"replica port {port} outside the loopback range"

        # Buffered JSON through the relay: status, content type, and body preserved.
        buffered = await coord.client.post(
            "/v1/chat/completions",
            json={"model": CHAT_MODEL, "_echo": "world",
                  "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert buffered.status_code == 200, daemon.stderr
        assert buffered.headers["content-type"].startswith("application/json")
        assert json.loads(buffered.content)["choices"][0]["message"]["content"] == "world"

        # Raw SSE through the relay: verbatim bytes, streaming media type preserved.
        streamed = await coord.client.post(
            "/v1/chat/completions",
            json={"model": CHAT_MODEL, "stream": True, "_echo": "Hiya",
                  "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {key}"},
        )
        assert streamed.status_code == 200, daemon.stderr
        assert streamed.headers["content-type"].startswith("text/event-stream")
        body = streamed.content
        assert b'"content":"Hi"' in body and b'"content":"ya"' in body
        assert body.endswith(b"data: [DONE]\n\n")

        rc = await daemon.stop()
    assert rc == 0, daemon.stderr
