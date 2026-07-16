"""Liveness transitions and replica routing filters, driven by FakeClock."""

from registry_helpers import (
    FakeClock,
    make_heartbeat,
    make_register_request,
    make_replica,
)

from fallow_coordinator.registry import SqliteRegistry
from fallow_protocol.messages import AgentState
from fallow_protocol.models import ReplicaState


async def _enrol(registry: SqliteRegistry, hostname: str) -> str:
    token = await registry.create_enrollment_token()
    response = await registry.register_agent(
        make_register_request(token, hostname=hostname), host="10.0.0.5"
    )
    return response.agent_id


async def test_snapshot_liveness_transitions(registry: SqliteRegistry, clock: FakeClock) -> None:
    agent_id = await _enrol(registry, "pc1")
    await registry.record_heartbeat(agent_id, make_heartbeat(agent_id))

    # Fresh: present and not suspect.
    fresh = await registry.snapshots(clock())
    assert len(fresh) == 1
    assert fresh[0].suspect is False

    # After >15s: suspect but still listed.
    clock.advance(20)
    suspect = await registry.snapshots(clock())
    assert len(suspect) == 1
    assert suspect[0].suspect is True
    assert await registry.list_offline(clock()) == ()

    # After >45s: offline, excluded from snapshots, surfaced by list_offline.
    clock.advance(30)
    assert await registry.snapshots(clock()) == ()
    assert await registry.list_offline(clock()) == (agent_id,)


async def test_replica_endpoints_filter_ready_idle_non_suspect(
    registry: SqliteRegistry, clock: FakeClock
) -> None:
    agent_id = await _enrol(registry, "pc1")
    await registry.record_heartbeat(
        agent_id,
        make_heartbeat(agent_id, state=AgentState.IDLE, replicas=(make_replica("m1"),)),
    )

    endpoints = await registry.replica_endpoints("m1", clock())
    assert len(endpoints) == 1
    assert endpoints[0].agent_id == agent_id
    assert endpoints[0].port == 8080
    assert (await registry.replica_endpoints("other", clock())) == ()


async def test_reclaimed_agent_excluded_from_routing(
    registry: SqliteRegistry, clock: FakeClock
) -> None:
    agent_id = await _enrol(registry, "pc1")
    await registry.record_heartbeat(
        agent_id,
        make_heartbeat(
            agent_id,
            state=AgentState.IDLE,
            replicas=(make_replica("m1"),),
            serving_paused=True,
        ),
    )

    assert (await registry.replica_endpoints("m1", clock())) == ()
    snapshot = (await registry.snapshots(clock()))[0]
    assert snapshot.serving_paused is True


async def test_release_restores_routing(registry: SqliteRegistry, clock: FakeClock) -> None:
    agent_id = await _enrol(registry, "pc1")
    ready = (make_replica("m1"),)
    await registry.record_heartbeat(
        agent_id, make_heartbeat(agent_id, replicas=ready, serving_paused=True)
    )
    assert (await registry.replica_endpoints("m1", clock())) == ()

    # A later heartbeat with serving_paused cleared brings the replica back.
    await registry.record_heartbeat(
        agent_id, make_heartbeat(agent_id, replicas=ready, serving_paused=False)
    )
    assert len(await registry.replica_endpoints("m1", clock())) == 1


async def test_active_agent_excluded_from_routing(
    registry: SqliteRegistry, clock: FakeClock
) -> None:
    agent_id = await _enrol(registry, "pc1")
    await registry.record_heartbeat(
        agent_id,
        make_heartbeat(agent_id, state=AgentState.ACTIVE, replicas=(make_replica("m1"),)),
    )
    assert (await registry.replica_endpoints("m1", clock())) == ()


async def test_suspended_replica_excluded_from_routing(
    registry: SqliteRegistry, clock: FakeClock
) -> None:
    agent_id = await _enrol(registry, "pc1")
    suspended = make_replica("m1", state=ReplicaState.SUSPENDED)
    await registry.record_heartbeat(
        agent_id, make_heartbeat(agent_id, state=AgentState.IDLE, replicas=(suspended,))
    )
    assert (await registry.replica_endpoints("m1", clock())) == ()


async def test_suspect_agent_excluded_from_routing(
    registry: SqliteRegistry, clock: FakeClock
) -> None:
    agent_id = await _enrol(registry, "pc1")
    await registry.record_heartbeat(
        agent_id,
        make_heartbeat(agent_id, state=AgentState.IDLE, replicas=(make_replica("m1"),)),
    )
    clock.advance(20)  # now suspect
    assert (await registry.replica_endpoints("m1", clock())) == ()


async def test_snapshot_omits_idle_prediction_until_reported(
    registry: SqliteRegistry, clock: FakeClock
) -> None:
    agent_id = await _enrol(registry, "pc1")
    await registry.record_heartbeat(agent_id, make_heartbeat(agent_id))

    snapshot = (await registry.snapshots(clock()))[0]
    assert snapshot.predicted_idle_remaining_s is None
    assert snapshot.predicted_idle_confidence is None


async def test_snapshot_records_and_exposes_idle_prediction(
    registry: SqliteRegistry, clock: FakeClock
) -> None:
    agent_id = await _enrol(registry, "pc1")
    await registry.record_heartbeat(
        agent_id,
        make_heartbeat(
            agent_id,
            predicted_idle_remaining_s=90.0,
            predicted_idle_confidence=0.75,
        ),
    )

    snapshot = (await registry.snapshots(clock()))[0]
    assert snapshot.predicted_idle_remaining_s == 90.0
    assert snapshot.predicted_idle_confidence == 0.75
