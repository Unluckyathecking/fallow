"""Preemption: the yield/resume/escalate state machine and its poll thread.

Public API:
- ``PreemptController`` — implements ``fallow_protocol.interfaces.Preemptor``.
- ``PollLoop`` — the dedicated thread that drives a ``Preemptor`` at the
  configured poll rate.
- ``ReclaimController`` — the sticky user-triggered takedown override.

See ``docs/adr/002-preemption.md`` and this package's ``README.md``.
"""

from fallow_agent.preempt.controller import PreemptController
from fallow_agent.preempt.poll_loop import PollLoop
from fallow_agent.preempt.reclaim import (
    ReclaimController,
    reclaim_control_path,
    request_reclaim,
    request_release,
)

__all__ = [
    "PollLoop",
    "PreemptController",
    "ReclaimController",
    "reclaim_control_path",
    "request_reclaim",
    "request_release",
]
