"""Table tests for CapabilityScheduler (arm c) — eligibility, ranking, replicas."""

from scheduler_helpers import make_agent, make_endpoint, make_replica_status

from fallow_coordinator.scheduler import CapabilityScheduler
from fallow_protocol.messages import AgentState
from fallow_protocol.models import ReplicaState


def test_excludes_non_idle_and_suspect_agents() -> None:
    policy = CapabilityScheduler()
    agents = [
        make_agent("active", state=AgentState.ACTIVE),
        make_agent("draining", state=AgentState.DRAINING),
        make_agent("suspect", suspect=True),
        make_agent("idle"),
    ]
    assert policy.select_agent("m1", needs_gpu=False, agents=agents) == "idle"


def test_returns_none_when_nobody_eligible() -> None:
    policy = CapabilityScheduler()
    agents = [make_agent("active", state=AgentState.ACTIVE), make_agent("s", suspect=True)]
    assert policy.select_agent("m1", needs_gpu=False, agents=agents) is None
    assert policy.select_agent("m1", needs_gpu=False, agents=[]) is None


def test_needs_gpu_excludes_gpuless_agents() -> None:
    policy = CapabilityScheduler()
    agents = [make_agent("cpu", gpu_count=0), make_agent("gpu", gpu_count=1)]
    assert policy.select_agent("m1", needs_gpu=True, agents=agents) == "gpu"
    # No GPU anywhere → wait.
    assert policy.select_agent("m1", needs_gpu=True, agents=[make_agent("cpu")]) is None


def test_warm_replica_beats_more_ram() -> None:
    policy = CapabilityScheduler()
    warm = make_agent(
        "warm", mem_available_mb=1, replicas=[make_replica_status("m1", ReplicaState.READY)]
    )
    roomy = make_agent("roomy", mem_available_mb=999_999)
    assert policy.select_agent("m1", needs_gpu=False, agents=[roomy, warm]) == "warm"


def test_suspended_replica_counts_as_warm() -> None:
    policy = CapabilityScheduler()
    warm = make_agent(
        "warm", mem_available_mb=1, replicas=[make_replica_status("m1", ReplicaState.SUSPENDED)]
    )
    roomy = make_agent("roomy", mem_available_mb=999)
    assert policy.select_agent("m1", needs_gpu=False, agents=[roomy, warm]) == "warm"


def test_loading_replica_does_not_count_as_warm() -> None:
    policy = CapabilityScheduler()
    loading = make_agent(
        "loading", mem_available_mb=1, replicas=[make_replica_status("m1", ReplicaState.LOADING)]
    )
    roomy = make_agent("roomy", mem_available_mb=999)
    assert policy.select_agent("m1", needs_gpu=False, agents=[loading, roomy]) == "roomy"


def test_warm_replica_must_match_model_id() -> None:
    policy = CapabilityScheduler()
    other = make_agent("other", mem_available_mb=1, replicas=[make_replica_status("other-model")])
    roomy = make_agent("roomy", mem_available_mb=999)
    assert policy.select_agent("m1", needs_gpu=False, agents=[other, roomy]) == "roomy"


def test_gpu_beats_no_gpu_when_no_warm_replica() -> None:
    policy = CapabilityScheduler()
    gpu = make_agent("gpu", gpu_count=1, mem_available_mb=1)
    cpu = make_agent("cpu", gpu_count=0, mem_available_mb=999_999)
    assert policy.select_agent("m1", needs_gpu=False, agents=[cpu, gpu]) == "gpu"


def test_ram_breaks_ties_when_capabilities_equal() -> None:
    policy = CapabilityScheduler()
    small = make_agent("small", mem_available_mb=100)
    big = make_agent("big", mem_available_mb=200)
    assert policy.select_agent("m1", needs_gpu=False, agents=[small, big]) == "big"


def test_agent_id_is_final_deterministic_tiebreak() -> None:
    policy = CapabilityScheduler()
    # Identical capability and RAM → lexicographically smallest agent_id wins,
    # regardless of input ordering.
    agents = [make_agent("c"), make_agent("a"), make_agent("b")]
    assert policy.select_agent("m1", needs_gpu=False, agents=agents) == "a"
    assert policy.select_agent("m1", needs_gpu=False, agents=list(reversed(agents))) == "a"


def test_pick_replica_least_inflight() -> None:
    policy = CapabilityScheduler()
    busy = make_endpoint(host="h1", port=1, inflight=5)
    idle = make_endpoint(host="h2", port=2, inflight=0)
    assert policy.pick_replica("m1", [busy, idle]) is idle


def test_pick_replica_tiebreak_host_port_lexicographic() -> None:
    policy = CapabilityScheduler()
    a = make_endpoint(host="h1", port=2, inflight=0)
    b = make_endpoint(host="h1", port=1, inflight=0)
    # Same inflight → "h1:1" < "h1:2" lexicographically.
    assert policy.pick_replica("m1", [a, b]) is b


def test_pick_replica_filters_by_model_and_handles_empty() -> None:
    policy = CapabilityScheduler()
    other = make_endpoint(host="h1", port=1, model_id="other")
    assert policy.pick_replica("m1", [other]) is None
    assert policy.pick_replica("m1", []) is None
