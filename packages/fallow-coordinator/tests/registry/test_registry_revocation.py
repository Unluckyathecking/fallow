"""Revocation: voided enrollment tokens and revoked device tokens (ADR 104)."""

from pathlib import Path

import pytest
from registry_helpers import (
    ADMIN_KEY,
    FakeClock,
    make_heartbeat,
    make_register_request,
    make_replica,
)

from fallow_coordinator.registry import (
    EnrollmentTokenError,
    RegistryConfig,
    SqliteRegistry,
    UnknownAgentError,
)
from fallow_protocol.messages import AgentState


async def _enrolled(registry: SqliteRegistry, hostname: str = "pc1") -> tuple[str, str]:
    token = await registry.create_enrollment_token()
    request = make_register_request(token, hostname)
    response = await registry.register_agent(request, host="10.0.0.5")
    return response.agent_id, response.device_token


async def test_revoked_enrollment_token_cannot_enrol(registry: SqliteRegistry) -> None:
    token = await registry.create_enrollment_token()
    (info,) = await registry.list_enrollment_tokens()
    assert info.state == "outstanding"

    assert await registry.revoke_enrollment_token(info.token_id) is True

    with pytest.raises(EnrollmentTokenError):
        await registry.register_agent(make_register_request(token), host="10.0.0.5")
    (after,) = await registry.list_enrollment_tokens()
    assert after.state == "revoked"


async def test_revoking_a_spent_or_unknown_token_reports_nothing_done(
    registry: SqliteRegistry,
) -> None:
    token = await registry.create_enrollment_token()
    await registry.register_agent(make_register_request(token), host="10.0.0.5")
    (info,) = await registry.list_enrollment_tokens()
    assert info.state == "used"

    assert await registry.revoke_enrollment_token(info.token_id) is False
    assert await registry.revoke_enrollment_token("000000000000") is False
    # A used token stays used: revocation never rewrites what an enrolment did.
    assert (await registry.list_enrollment_tokens())[0].state == "used"


async def test_revoked_device_token_stops_authenticating(registry: SqliteRegistry) -> None:
    agent_id, device_token = await _enrolled(registry)
    assert await registry.authenticate_agent(device_token) == agent_id

    await registry.revoke_agent(agent_id)

    assert await registry.authenticate_agent(device_token) is None


async def test_revoke_is_idempotent_and_unknown_agents_raise(registry: SqliteRegistry) -> None:
    agent_id, _ = await _enrolled(registry)
    await registry.revoke_agent(agent_id)
    await registry.revoke_agent(agent_id)

    with pytest.raises(UnknownAgentError):
        await registry.revoke_agent("no-such-agent")


async def test_revoked_agent_leaves_the_routing_views(
    registry: SqliteRegistry, clock: FakeClock
) -> None:
    agent_id, _ = await _enrolled(registry)
    await registry.record_heartbeat(
        agent_id, make_heartbeat(agent_id, AgentState.IDLE, (make_replica("qwen2.5-7b"),))
    )
    assert await registry.replica_endpoints("qwen2.5-7b", clock())

    await registry.revoke_agent(agent_id)

    assert await registry.replica_endpoints("qwen2.5-7b", clock()) == ()
    assert await registry.snapshots(clock()) == ()


async def test_revocation_survives_a_restart(clock: FakeClock, tmp_path: Path) -> None:
    config = RegistryConfig(admin_key=ADMIN_KEY)
    db_path = tmp_path / "registry.db"
    async with SqliteRegistry(db_path, config, clock) as first:
        agent_id, device_token = await _enrolled(first)
        spare = await first.create_enrollment_token()
        (info,) = (t for t in await first.list_enrollment_tokens() if t.state == "outstanding")
        await first.revoke_agent(agent_id)
        await first.revoke_enrollment_token(info.token_id)

    async with SqliteRegistry(db_path, config, clock) as second:
        assert await second.authenticate_agent(device_token) is None
        with pytest.raises(EnrollmentTokenError):
            await second.register_agent(make_register_request(spare), host="10.0.0.5")
