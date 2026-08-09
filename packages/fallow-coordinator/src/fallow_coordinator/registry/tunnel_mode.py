"""Site/legacy routing mode and pure eligibility predicates."""

from enum import StrEnum

from fallow_protocol.messages import AgentSnapshot, AgentState
from fallow_protocol.models import ReplicaState


class EnrollmentMode(StrEnum):
    LEGACY = "legacy"
    SITE = "site"


class Transport(StrEnum):
    DIRECT = "direct"
    SITE_RELAY = "site_relay"


def transport_for_mode(mode: str) -> Transport:
    return Transport.SITE_RELAY if mode == EnrollmentMode.SITE else Transport.DIRECT


def site_eligible(agent: AgentSnapshot, model_id: str, *, claim_waiter: bool) -> bool:
    """Return true only for a fresh, idle, unpaused site relay replica."""
    return (
        agent.state == AgentState.IDLE
        and not agent.suspect
        and not agent.serving_paused
        and claim_waiter
        and any(r.model_id == model_id and r.state == ReplicaState.READY for r in agent.replicas)
    )
