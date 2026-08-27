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
    RevokedAgentError,
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


async def test_token_ids_are_normalized_and_malformed_ones_are_named(
    registry: SqliteRegistry,
) -> None:
    """Case and whitespace are noise; anything else is a typo, and says so.

    A typo that fell through to the ``substr`` comparison would match nothing and
    read as "already spent" — which is the one answer that stops an operator
    looking while a live join file is still out there.
    """
    await registry.create_enrollment_token()
    (info,) = await registry.list_enrollment_tokens()

    assert await registry.revoke_enrollment_token(f"  {info.token_id.upper()}  ") is True

    for bad in ("", "deadbeef", "deadbeefcafeb", "deadbeefcafg", "../../etc", "dead beef cafe"):
        with pytest.raises(EnrollmentTokenError, match="is not a token id"):
            await registry.revoke_enrollment_token(bad)


async def test_revoked_device_token_stops_authenticating(registry: SqliteRegistry) -> None:
    agent_id, device_token = await _enrolled(registry)
    assert await registry.authenticate_agent(device_token) == agent_id

    await registry.revoke_agent(agent_id)

    with pytest.raises(RevokedAgentError):
        await registry.authenticate_agent(device_token)


async def test_a_revoked_token_is_not_the_same_failure_as_an_unknown_one(
    registry: SqliteRegistry,
) -> None:
    """Both fail, but only one is a decision somebody made about this machine.

    A coordinator restored from an older backup has forgotten every device token
    it ever issued; if that read as revocation the whole fleet would record it
    permanently (ADR 104).
    """
    agent_id, device_token = await _enrolled(registry)
    assert await registry.authenticate_agent("never-issued") is None

    await registry.revoke_agent(agent_id)

    with pytest.raises(RevokedAgentError) as caught:
        await registry.authenticate_agent(device_token)
    assert caught.value.agent_id == agent_id


async def test_revoked_agents_stay_listable(registry: SqliteRegistry) -> None:
    """Leaving routing is not the same as vanishing.

    A revoked row is out of ``snapshots`` at once, which is right for routing and
    is exactly why it needs a surface of its own: without one, a revoked desk
    reads the same as a desk that was never enrolled.
    """
    first, _ = await _enrolled(registry, "pc1")
    second, _ = await _enrolled(registry, "pc2")
    assert await registry.list_revoked_agents() == ()

    await registry.revoke_agent(second)

    (row,) = await registry.list_revoked_agents()
    assert (row.agent_id, row.hostname) == (second, "pc2")
    assert row.revoked_at is not None

    await registry.revoke_agent(first)
    assert {row.agent_id for row in await registry.list_revoked_agents()} == {first, second}


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
        with pytest.raises(RevokedAgentError):
            await second.authenticate_agent(device_token)
        with pytest.raises(EnrollmentTokenError):
            await second.register_agent(make_register_request(spare), host="10.0.0.5")
