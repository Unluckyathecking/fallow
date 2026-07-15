"""First-run enrollment / identity resolution.

On the first start a machine has no identity: it registers with the coordinator
using the one-time enrollment token and persists the returned ``agent_id`` +
``device_token`` (0600, atomic). On every later start it loads that file and
skips registration entirely — a machine enrolls exactly once.

The initial :class:`AgentConfig` comes from the registration response; on a
loaded-from-disk start it defaults (heartbeat 5s, idle 120s, …) and is refreshed
by the first heartbeat response's ``config`` field.
"""

from __future__ import annotations

import httpx

from fallow_agent.heartbeat import CoordinatorClient
from fallow_agent.main.errors import IdentityError
from fallow_agent.main.identity import IdentityState, load_identity, save_identity
from fallow_agent.main.seams import CapsFactory
from fallow_agent.main.settings import AgentSettings
from fallow_protocol.messages import AgentConfig, RegisterRequest
from fallow_protocol.version import PROTOCOL_VERSION


async def resolve_identity(
    settings: AgentSettings,
    client: httpx.AsyncClient,
    *,
    caps_factory: CapsFactory,
) -> tuple[IdentityState, AgentConfig]:
    """Load the persisted identity, or enroll and persist a new one.

    Returns the identity plus the initial :class:`AgentConfig`. Raises
    :class:`IdentityError` when unenrolled and no enrollment token is configured.
    """
    existing = load_identity(settings.state_path)
    if existing is not None:
        return existing, AgentConfig()
    if not settings.enrollment_token:
        raise IdentityError(
            "no persisted identity and no enrollment_token configured; cannot enroll"
        )
    caps = caps_factory(settings.agent_version)
    coordinator = CoordinatorClient(base_url=settings.coordinator_url, client=client)
    response = await coordinator.register(
        RegisterRequest(
            enrollment_token=settings.enrollment_token,
            protocol_version=PROTOCOL_VERSION,
            caps=caps,
        )
    )
    state = IdentityState(agent_id=response.agent_id, device_token=response.device_token)
    save_identity(settings.state_path, state)
    return state, response.config
