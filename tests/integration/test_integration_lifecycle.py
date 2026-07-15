"""Scenario 1 — lifecycle: mint → I2 enroll (persisted 0600) → A5 heartbeat →
the agent shows up in the admin snapshot as IDLE with its READY replica.

This is the one scenario that drives I2's ``resolve_identity`` directly (the
real first-run register-and-persist path) against the live coordinator app, then
uses the real A5 client for the IDLE heartbeat.
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path

from integration_helpers import (
    CHAT_MODEL,
    LOOPBACK,
    Harness,
    HarnessFactory,
    credentialed_client,
    heartbeat,
    list_agents,
    make_caps,
    make_replica,
    mint_enrollment_token,
)

from fallow_agent.main import AgentSettings, resolve_identity
from fallow_protocol.messages import AgentState
from fallow_protocol.models import ReplicaState


def _settings(tmp_path: Path, token: str) -> AgentSettings:
    return AgentSettings.model_validate(
        {
            "coordinator_url": "http://coord",
            "bind_host": LOOPBACK,
            "llama_server_binary": tmp_path / "llama-server",
            "enrollment_token": token,
            "state_path": tmp_path / "agent-state.json",
        }
    )


async def test_lifecycle_enroll_heartbeat_visible(
    make_harness: HarnessFactory, tmp_path: Path
) -> None:
    harness: Harness = await make_harness()
    token = await mint_enrollment_token(harness.client)

    # I2 first-run enrollment against the live coordinator, persisted 0600.
    settings = _settings(tmp_path, token)
    identity, _config = await resolve_identity(settings, harness.client, caps_factory=make_caps)
    assert identity.agent_id
    state_file = settings.state_path
    assert state_file.exists()
    if sys.platform != "win32":  # Windows has no POSIX modes
        assert stat.S_IMODE(state_file.stat().st_mode) == 0o600

    # A5 heartbeat carrying a READY replica; identity holds the device token.
    client = credentialed_client(harness.client, identity.agent_id, identity.device_token)
    replica = make_replica(CHAT_MODEL, port=8080, state=ReplicaState.READY)
    hb_resp = await heartbeat(client, state=AgentState.IDLE, replicas=(replica,))
    assert hb_resp.desired_models == ()

    agents = await list_agents(harness.client)
    assert [a.agent_id for a in agents] == [identity.agent_id]
    snap = agents[0]
    assert snap.state == AgentState.IDLE
    assert [r.model_id for r in snap.replicas] == [CHAT_MODEL]
    assert snap.replicas[0].state == ReplicaState.READY
