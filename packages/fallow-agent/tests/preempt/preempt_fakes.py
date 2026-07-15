"""Deterministic fakes for the preemption tests.

No network, no real llama-server, no GPU, no real clock. Every collaborator is
an in-memory recorder so tests assert on ordering and arguments exactly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fallow_protocol.interfaces import EventSink, IdleDetector, ProcessSupervisor
from fallow_protocol.messages import AgentEvent, AgentState
from fallow_protocol.models import ModelManifest, ReplicaState, ReplicaStatus


class FakeClock:
    """A settable monotonic source. ``t`` is what the next call returns."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def fixed_now() -> datetime:
    """A frozen wall clock so event timestamps are deterministic."""
    return datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)


class RecordingSupervisor(ProcessSupervisor):
    """Records call order into a shared log and serves canned statuses."""

    def __init__(
        self,
        log: list[str],
        statuses: tuple[ReplicaStatus, ...] = (),
    ) -> None:
        self._log = log
        self._statuses = statuses
        self.stopped: list[str] = []

    def start_replica(self, manifest: ModelManifest, model_path: Path, port: int) -> None:
        self._log.append("start_replica")

    def stop_replica(self, model_id: str) -> None:
        self._log.append(f"stop_replica:{model_id}")
        self.stopped.append(model_id)

    def suspend_all(self) -> None:
        self._log.append("suspend_all")

    def resume_all(self) -> None:
        self._log.append("resume_all")

    def statuses(self) -> tuple[ReplicaStatus, ...]:
        return self._statuses


class RecordingSink(EventSink):
    """Records emitted events into a shared log and its own list."""

    def __init__(self, log: list[str]) -> None:
        self._log = log
        self.events: list[AgentEvent] = []

    def emit(self, event: AgentEvent) -> None:
        self._log.append(f"emit:{event.kind.value}")
        self.events.append(event)


class RaisingDetector(IdleDetector):
    """A detector that always raises after counting the call."""

    def __init__(self) -> None:
        self.calls = 0

    def seconds_since_input(self) -> float:
        self.calls += 1
        raise RuntimeError("detector boom")


class ConstantDetector(IdleDetector):
    """A detector returning a fixed idle value, counting each call."""

    def __init__(self, idle_s: float) -> None:
        self.idle_s = idle_s
        self.calls = 0

    def seconds_since_input(self) -> float:
        self.calls += 1
        return self.idle_s


class RecordingController:
    """Implements the Preemptor contract; records on_poll arguments."""

    def __init__(self) -> None:
        self.polls: list[tuple[float, float]] = []
        self._state = AgentState.IDLE

    @property
    def state(self) -> AgentState:
        return self._state

    def on_poll(self, idle_s: float, monotonic_now: float) -> AgentState:
        self.polls.append((idle_s, monotonic_now))
        return self._state


def suspended(model_id: str, port: int, gpu: bool = True) -> ReplicaStatus:
    return ReplicaStatus(model_id=model_id, port=port, state=ReplicaState.SUSPENDED, gpu=gpu)


def stopped_status(model_id: str, port: int) -> ReplicaStatus:
    return ReplicaStatus(model_id=model_id, port=port, state=ReplicaState.STOPPED)
