"""Tests for RoundRobinScheduler (arm b) — fair rotation, capability-blindness."""

from scheduler_helpers import make_agent, make_endpoint, make_replica_status

from fallow_coordinator.scheduler import RoundRobinScheduler
from fallow_protocol.messages import AgentState
from fallow_protocol.models import ReplicaState


def test_cycles_fairly_over_sorted_agents() -> None:
    policy = RoundRobinScheduler()
    agents = [make_agent("b"), make_agent("a"), make_agent("c")]
    picks = [policy.select_agent("m1", needs_gpu=False, agents=agents) for _ in range(6)]
    # Sorted by agent_id, then repeat: a, b, c, a, b, c.
    assert picks == ["a", "b", "c", "a", "b", "c"]


def test_ignores_capability_signals() -> None:
    policy = RoundRobinScheduler()
    # A warm, GPU-rich, high-RAM agent gets no preference over a bare one.
    strong = make_agent(
        "a",
        gpu_count=4,
        mem_available_mb=999_999,
        replicas=[make_replica_status("m1", ReplicaState.READY)],
    )
    weak = make_agent("b", gpu_count=0, mem_available_mb=1)
    picks = [policy.select_agent("m1", needs_gpu=False, agents=[strong, weak]) for _ in range(4)]
    assert picks == ["a", "b", "a", "b"]


def test_still_applies_eligibility_gate() -> None:
    policy = RoundRobinScheduler()
    agents = [
        make_agent("active", state=AgentState.ACTIVE),
        make_agent("idle1"),
        make_agent("idle2"),
    ]
    picks = [policy.select_agent("m1", needs_gpu=False, agents=agents) for _ in range(4)]
    assert picks == ["idle1", "idle2", "idle1", "idle2"]


def test_needs_gpu_gate_still_enforced() -> None:
    policy = RoundRobinScheduler()
    agents = [make_agent("cpu", gpu_count=0), make_agent("gpu", gpu_count=1)]
    picks = [policy.select_agent("m1", needs_gpu=True, agents=agents) for _ in range(3)]
    assert picks == ["gpu", "gpu", "gpu"]


def test_returns_none_when_nobody_eligible() -> None:
    policy = RoundRobinScheduler()
    assert policy.select_agent("m1", needs_gpu=False, agents=[]) is None


def test_reset_restores_deterministic_start() -> None:
    policy = RoundRobinScheduler()
    agents = [make_agent("a"), make_agent("b")]
    assert policy.select_agent("m1", needs_gpu=False, agents=agents) == "a"
    assert policy.select_agent("m1", needs_gpu=False, agents=agents) == "b"
    policy.reset()
    assert policy.select_agent("m1", needs_gpu=False, agents=agents) == "a"


def test_pick_replica_rotates() -> None:
    policy = RoundRobinScheduler()
    r0 = make_endpoint(host="h", port=1)
    r1 = make_endpoint(host="h", port=2)
    picks = [policy.pick_replica("m1", [r1, r0]) for _ in range(4)]
    # Sorted by host:port → r0 ("h:1"), r1 ("h:2"), repeating.
    assert picks == [r0, r1, r0, r1]


def test_pick_replica_empty_returns_none() -> None:
    policy = RoundRobinScheduler()
    assert policy.pick_replica("m1", []) is None
