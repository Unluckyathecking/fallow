"""Idle detection (module A1).

Answers exactly one question — "how many seconds since the user last touched
this machine, right now?" — in microseconds, without spawning a process or
blocking. Polling/threading and the yield/resume state machine live in the
preemption module, not here.

Public API:
- `create_idle_detector()` — the factory the agent uses in production.
- `WindowsIdleDetector` / `DarwinIdleDetector` / `LinuxIdleDetector` — the
  per-OS implementations of `fallow_protocol.interfaces.IdleDetector`.
- `FakeIdleDetector` — deterministic, thread-safe fake for tests and the bench
  churn injector.
- `IdlePredictor` / `IdlePrediction` — optional near-future idle prediction on
  top of any `IdleDetector` (off by default; see ADR 049).
"""

from fallow_agent.idle.darwin import DarwinIdleDetector
from fallow_agent.idle.factory import ConstantIdleDetector, create_idle_detector
from fallow_agent.idle.fake import FakeIdleDetector
from fallow_agent.idle.linux import LinuxIdleDetector
from fallow_agent.idle.predictor import IdlePrediction, IdlePredictor
from fallow_agent.idle.windows import WindowsIdleDetector

__all__ = [
    "ConstantIdleDetector",
    "DarwinIdleDetector",
    "FakeIdleDetector",
    "IdlePrediction",
    "IdlePredictor",
    "LinuxIdleDetector",
    "WindowsIdleDetector",
    "create_idle_detector",
]
