"""Tests for select_for_poll — the per-long-poll model-id pre-filter."""

from conftest import make_agent

from fallow_coordinator.scheduler import CapabilityScheduler, RoundRobinScheduler, select_for_poll
from fallow_protocol.messages import AgentState

MODELS = ("m1", "m2", "m3")


def test_fit_agent_may_lease_all_assigned_models() -> None:
    policy = CapabilityScheduler()
    agent = make_agent("idle")
    assert select_for_poll(agent, MODELS, policy) == MODELS


def test_non_idle_agent_gets_empty_list() -> None:
    policy = CapabilityScheduler()
    for state in (AgentState.ACTIVE, AgentState.DRAINING):
        agent = make_agent("a", state=state)
        assert select_for_poll(agent, MODELS, policy) == ()


def test_suspect_agent_gets_empty_list() -> None:
    policy = CapabilityScheduler()
    agent = make_agent("a", suspect=True)
    assert select_for_poll(agent, MODELS, policy) == ()


def test_empty_model_list_stays_empty() -> None:
    policy = CapabilityScheduler()
    assert select_for_poll(make_agent("idle"), (), policy) == ()


def test_result_is_a_tuple_regardless_of_input_sequence() -> None:
    policy = CapabilityScheduler()
    assert select_for_poll(make_agent("idle"), ["m1", "m2"], policy) == ("m1", "m2")


def test_does_not_mutate_round_robin_cursor() -> None:
    # select_for_poll must be side-effect-free on the policy so it never perturbs
    # arm-b's rotation fairness.
    policy = RoundRobinScheduler()
    agent = make_agent("idle")
    select_for_poll(agent, MODELS, policy)
    select_for_poll(agent, MODELS, policy)
    agents = [make_agent("a"), make_agent("b")]
    assert policy.select_agent("m1", needs_gpu=False, agents=agents) == "a"
