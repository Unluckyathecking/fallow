"""Enrollment-token lifecycle: single-use, mismatch, and protocol guarding."""

import pytest
from registry_helpers import make_register_request

from fallow_coordinator.registry import (
    EnrollmentTokenError,
    ProtocolMismatchError,
    SqliteRegistry,
)


async def test_valid_token_registers_and_returns_config(registry: SqliteRegistry) -> None:
    token = await registry.create_enrollment_token()
    response = await registry.register_agent(make_register_request(token), host="10.0.0.5")

    assert response.agent_id
    assert response.device_token
    assert response.config.assigned_models == ()


async def test_enrollment_token_is_single_use(registry: SqliteRegistry) -> None:
    token = await registry.create_enrollment_token()
    await registry.register_agent(make_register_request(token), host="10.0.0.5")

    with pytest.raises(EnrollmentTokenError):
        await registry.register_agent(make_register_request(token), host="10.0.0.6")


async def test_unknown_token_is_rejected(registry: SqliteRegistry) -> None:
    with pytest.raises(EnrollmentTokenError):
        await registry.register_agent(make_register_request("never-minted"), host="10.0.0.5")


async def test_protocol_mismatch_rejected_before_token_consumed(
    registry: SqliteRegistry,
) -> None:
    token = await registry.create_enrollment_token()
    request = make_register_request(token, protocol_version=999)

    with pytest.raises(ProtocolMismatchError):
        await registry.register_agent(request, host="10.0.0.5")

    # The token must NOT have been consumed by the rejected attempt.
    ok = await registry.register_agent(make_register_request(token), host="10.0.0.5")
    assert ok.agent_id


async def test_device_token_authenticates_the_new_agent(registry: SqliteRegistry) -> None:
    token = await registry.create_enrollment_token()
    response = await registry.register_agent(make_register_request(token), host="10.0.0.5")

    assert await registry.authenticate_agent(response.device_token) == response.agent_id
    assert await registry.authenticate_agent("wrong-token") is None
