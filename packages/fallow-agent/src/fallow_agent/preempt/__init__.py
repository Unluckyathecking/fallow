"""Preemption: the yield/resume/escalate state machine and its poll thread.

Public API:
- ``PreemptController`` — implements ``fallow_protocol.interfaces.Preemptor``.
- ``PollLoop`` — the dedicated thread that drives a ``Preemptor`` at the
  configured poll rate.

See ``docs/adr/002-preemption.md`` and this package's ``README.md``.
"""

from fallow_agent.preempt.controller import PreemptController
from fallow_agent.preempt.poll_loop import PollLoop

__all__ = ["PollLoop", "PreemptController"]
