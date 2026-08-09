"""Site-mode eligibility is fail-closed."""

from registry_helpers import make_caps, make_replica

from fallow_coordinator.registry.tunnel_mode import site_eligible
from fallow_protocol.messages import AgentSnapshot, AgentState
from fallow_protocol.models import ReplicaState


def agent(**kw):
    return AgentSnapshot(
        agent_id="a",
        host="h",
        state=AgentState.IDLE,
        suspect=False,
        caps=make_caps(),
        mem_available_mb=1,
        replicas=(make_replica("m", ReplicaState.READY),),
        **kw,
    )


def test_requires_active_claim_and_not_paused():
    assert not site_eligible(agent(), "m", claim_waiter=False)
    assert not site_eligible(agent(serving_paused=True), "m", claim_waiter=True)
    assert site_eligible(agent(), "m", claim_waiter=True)
