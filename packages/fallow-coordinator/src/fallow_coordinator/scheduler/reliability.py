"""Empirical per-agent task-success reliability estimator (scheduler module C7).

The idle-survival model (``churn_model.py``) answers "will this machine stay
idle?" It says nothing about whether an agent actually *finishes* the work it
leases. A machine can be reliably idle yet keep crashing or churning mid-unit,
so its leases expire and requeue: idle-reliable, task-unreliable. BOINC-style
volunteer computing tracks exactly this host reliability, for exactly this
reason.

This module derives one number per agent from the unit lifecycle log
(``units.jsonl``, ADR 024): the fraction of that agent's *terminal* unit
outcomes that were completions.

    reliability(agent) = completed / (completed + dropped)

A ``WorkUnitState.DONE`` transition is a completion (success). A lease that
expired back to the queue (requeue → ``PENDING``) or exhausted its retries
(``DEAD``) is a drop, charged to the agent that held the lease. A ``LEASED``
transition is a handout, not a terminal outcome, so it counts toward neither.

Agents with no terminal history get an **optimistic prior** (default 0.9),
matching how :class:`ChurnModel` treats unseen agents: a new agent is not
starved of work for lack of a track record.

Purity: this module performs no I/O. :func:`build_reliability_model` consumes
already-parsed ``units.jsonl`` mappings (the app layer owns the file read), so
construction is deterministic and replayable.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from fallow_protocol.messages import WorkUnitState

# Reliability returned for an agent with no terminal outcomes on record.
# "Optimistic" = assume the agent finishes work, so a never-seen machine is not
# penalised purely for lack of history (mirrors ChurnModel's prior).
DEFAULT_RELIABILITY_PRIOR = 0.9

# Terminal transition states that count as a drop (a unit the agent was leased
# but did not complete): a requeue back to the pending pool, or a dead unit.
_DROP_STATES = frozenset({WorkUnitState.PENDING, WorkUnitState.DEAD})

# Per-agent tally: (completed, dropped).
_Counts = tuple[int, int]


@dataclass(frozen=True)
class ReliabilityModel:
    """Frozen empirical task-success estimator, keyed by ``agent_id``.

    ``by_agent`` maps an agent to its ``(completed, dropped)`` terminal-outcome
    counts. Build instances with :func:`build_reliability_model`, never by hand.
    """

    by_agent: Mapping[str, _Counts]
    optimistic_prior: float = DEFAULT_RELIABILITY_PRIOR

    def success_rate(self, agent_id: str) -> float:
        """Fraction of ``agent_id``'s terminal units that completed.

        Falls back to the optimistic prior when the agent has no terminal
        outcome on record (never leased a unit to completion or drop).
        """
        completed, dropped = self.by_agent.get(agent_id, (0, 0))
        total = completed + dropped
        if total == 0:
            return self.optimistic_prior
        return completed / total


def build_reliability_model(
    transitions: Iterable[Mapping[str, object]],
    *,
    optimistic_prior: float = DEFAULT_RELIABILITY_PRIOR,
) -> ReliabilityModel:
    """Build a :class:`ReliabilityModel` from parsed ``units.jsonl`` mappings.

    Each transition's ``state`` decides the tally for its ``agent_id``: ``done``
    is a completion, a requeue (``pending``) or ``dead`` is a drop, and every
    other state (notably ``leased``) is skipped. Rows missing a string
    ``agent_id`` or an unrecognised ``state`` are ignored, so a malformed or
    partially-written log line never derails the build.
    """
    tally: dict[str, list[int]] = {}
    for transition in transitions:
        agent_id = transition.get("agent_id")
        if not isinstance(agent_id, str) or not agent_id:
            continue
        outcome = _classify(transition.get("state"))
        if outcome is None:
            continue
        counts = tally.setdefault(agent_id, [0, 0])
        counts[outcome] += 1
    return ReliabilityModel(
        by_agent={agent_id: (done, dropped) for agent_id, (done, dropped) in tally.items()},
        optimistic_prior=optimistic_prior,
    )


def _classify(state: object) -> int | None:
    """Tally index for a transition ``state``: 0 completed, 1 dropped, else None."""
    if not isinstance(state, str):
        return None
    try:
        unit_state = WorkUnitState(state)
    except ValueError:
        return None
    if unit_state is WorkUnitState.DONE:
        return 0
    if unit_state in _DROP_STATES:
        return 1
    return None
