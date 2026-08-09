"""Site presence fencing and routing persistence."""

import pytest
from registry_helpers import make_heartbeat, make_register_request, make_replica

from fallow_coordinator.registry import EnrollmentMode, Transport
from fallow_protocol.messages import AgentState


async def enroll(registry, *, mode="legacy"):
    token = await registry.create_enrollment_token(mode=mode)
    response = await registry.register_agent(make_register_request(token), host="10.0.0.5")
    return response.agent_id


async def test_legacy_and_site_transport(registry):
    await enroll(registry)
    await enroll(registry, mode="site")
    cur = await registry._conn.execute(
        "SELECT transport FROM registry_agents ORDER BY registered_at"
    )
    assert [r["transport"] for r in await cur.fetchall()] == [
        Transport.DIRECT,
        Transport.SITE_RELAY,
    ]
    assert EnrollmentMode.SITE.value == "site"


async def test_presence_fence_rejects_older_and_duplicate(registry):
    agent = await enroll(registry)
    assert await registry.apply_presence_event(agent, "user_returned", 5) == 1
    assert await registry.apply_presence_event(agent, "user_idle", 4) == 1
    assert await registry.apply_presence_event(agent, "user_returned", 5) == 1
    row = await (
        await registry._conn.execute(
            "SELECT state, presence_sequence FROM registry_agents WHERE agent_id = ?", (agent,)
        )
    ).fetchone()
    assert row["state"] == AgentState.ACTIVE.value and row["presence_sequence"] == 5


async def test_newer_presence_fences_delayed_heartbeat(registry):
    agent = await enroll(registry, mode="site")
    await registry.apply_presence_event(agent, "user_returned", 8)
    await registry.record_heartbeat(agent, make_heartbeat(agent, state=AgentState.IDLE))
    snapshot = (await registry.snapshots(registry._now()))[0]
    assert snapshot.state == AgentState.ACTIVE


async def test_reclaim_generation_and_pause(registry):
    agent = await enroll(registry)
    await registry.record_heartbeat(agent, make_heartbeat(agent, replicas=(make_replica("m"),)))
    assert await registry.apply_presence_event(agent, "reclaim", 2) == 1
    assert await registry.replica_endpoints("m", registry._now()) == ()


@pytest.mark.parametrize("bad", ["wat", "", "user_idle"])
async def test_invalid_token_mode_rejected(registry, bad):
    with pytest.raises(ValueError):
        await registry.create_enrollment_token(mode=bad)


async def test_direct_heartbeat_sequence_reset_still_updates(registry):
    agent = await enroll(registry)
    await registry.apply_presence_event(agent, "user_returned", 8)
    await registry.record_heartbeat(agent, make_heartbeat(agent, state=AgentState.IDLE))
    assert (await registry.snapshots(registry._now()))[0].state == AgentState.IDLE


async def test_site_higher_restart_sequence_restores_updates(registry):
    agent = await enroll(registry, mode="site")
    await registry.apply_presence_event(agent, "user_returned", 8)
    await registry.record_heartbeat(agent, make_heartbeat(agent, state=AgentState.IDLE))
    assert (await registry.snapshots(registry._now()))[0].state == AgentState.ACTIVE
    heartbeat = make_heartbeat(agent, state=AgentState.IDLE)
    heartbeat = heartbeat.model_copy(update={"seq": 9})
    await registry.record_heartbeat(agent, heartbeat)
    assert (await registry.snapshots(registry._now()))[0].state == AgentState.IDLE
