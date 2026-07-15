"""Interface contracts between Fallow modules.

These ABCs are the seams along which modules are built (and along which the
future Go/Rust port will happen). Modules depend on these abstractions, never
on each other's concrete classes.

Threading/async conventions (fixed for v0.1):
- Agent hot path (idle poll → preemption) is SYNCHRONOUS and runs on a
  dedicated thread; implementations of IdleDetector.seconds_since_input and
  ProcessSupervisor.suspend_all/resume_all must be non-blocking (no network,
  no subprocess spawning, O(ms) worst case).
- Coordinator-side stores are ASYNC (asyncio + aiosqlite).
- EventSink.emit must never block the caller; buffer and ship in the
  background.
"""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path

from fallow_protocol.messages import (
    AgentEvent,
    AgentSnapshot,
    AgentState,
    JobStatus,
    JobSubmit,
    ReplicaEndpoint,
    WorkResult,
    WorkUnitLease,
    WorkUnitSpec,
)
from fallow_protocol.models import ModelManifest, ReplicaStatus

# ── Agent-side contracts ─────────────────────────────────────────────────────


class IdleDetector(ABC):
    """Reports how long since the user last touched the machine.

    Called at ~10Hz from the poll thread: implementations must cost
    microseconds and must never spawn a process.
    """

    @abstractmethod
    def seconds_since_input(self) -> float: ...


class ProcessSupervisor(ABC):
    """Owns all fallow-launched inference child processes on a machine."""

    @abstractmethod
    def start_replica(self, manifest: ModelManifest, model_path: Path, port: int) -> None:
        """Launch (async in the background) a replica; visible in statuses()
        as LOADING until healthy."""

    @abstractmethod
    def stop_replica(self, model_id: str) -> None: ...

    @abstractmethod
    def suspend_all(self) -> None:
        """Suspend every child immediately. Hot-path: must return in <10ms
        and must not touch the network."""

    @abstractmethod
    def resume_all(self) -> None: ...

    @abstractmethod
    def statuses(self) -> tuple[ReplicaStatus, ...]: ...


class Preemptor(ABC):
    """The yield/resume state machine. Driven by the poll thread; decides
    when to suspend, escalate (kill to free VRAM), and resume."""

    @abstractmethod
    def on_poll(self, idle_s: float, monotonic_now: float) -> AgentState:
        """Advance the state machine one tick; returns the resulting state.
        Must itself be hot-path safe (suspension happens inside this call)."""

    @property
    @abstractmethod
    def state(self) -> AgentState: ...


class ModelStore(ABC):
    """Agent-side model cache: pull-with-resume from the coordinator,
    verify sha256, keep on local disk."""

    @abstractmethod
    def path_if_present(self, manifest: ModelManifest) -> Path | None:
        """Return the verified local path, or None if absent/corrupt."""

    @abstractmethod
    async def ensure(self, manifest: ModelManifest) -> Path:
        """Download (resuming partial files), verify, and return the path."""


class EventSink(ABC):
    """Where agent modules report events (to the coordinator + local JSONL).
    emit() must never block the caller."""

    @abstractmethod
    def emit(self, event: AgentEvent) -> None: ...


# ── Coordinator-side contracts ───────────────────────────────────────────────


class QueueStore(ABC):
    """Durable job/work-unit store with leasing. Single source of truth is
    SQLite (WAL); every method is crash-safe."""

    @abstractmethod
    async def submit_job(self, job: JobSubmit, units: Sequence[WorkUnitSpec]) -> str:
        """Persist a job and its units; returns job_id. Units whose
        work_unit_id already has a stored result are recorded as DONE
        immediately (content-addressed dedup)."""

    @abstractmethod
    async def job_status(self, job_id: str) -> JobStatus | None: ...

    @abstractmethod
    async def lease_next(self, agent_id: str, model_ids: Sequence[str]) -> WorkUnitLease | None:
        """Atomically lease the next pending unit runnable with one of the
        given models, or None."""

    @abstractmethod
    async def extend_leases(self, agent_id: str, unit_ids: Sequence[str]) -> None: ...

    @abstractmethod
    async def result_upload_attempt(self, agent_id: str, work_unit_id: str) -> int | None:
        """Return the active attempt only when the agent currently holds the lease."""

    @abstractmethod
    async def bind_result_payload(
        self,
        agent_id: str,
        work_unit_id: str,
        attempt: int,
        digest: str,
        result_ref: str,
    ) -> bool:
        """Durably bind a payload when the lease still matches its preflight snapshot."""

    @abstractmethod
    async def complete_unit(self, agent_id: str, attempt: int, result: WorkResult) -> bool:
        """Record a result. Duplicate completions for the same unit are
        accepted no-ops; return false when the lease or payload binding does not match."""

    @abstractmethod
    async def completed_result_ref(self, work_unit_id: str) -> str | None:
        """Return the accepted payload reference for a successful completion."""

    @abstractmethod
    async def requeue_expired(self) -> int:
        """Requeue units with expired leases (attempts += 1; DEAD after the
        retry budget). Returns number requeued."""

    @abstractmethod
    async def requeue_agent(self, agent_id: str) -> int:
        """Requeue all units leased to an agent that went offline."""


class SchedulerPolicy(ABC):
    """Pure decision logic: no I/O, no clocks — fully deterministic given its
    inputs, so experiment arms are swappable and unit-testable."""

    @abstractmethod
    def select_agent(
        self, requirements_model_id: str, needs_gpu: bool, agents: Sequence[AgentSnapshot]
    ) -> str | None:
        """Choose which agent should receive a batch unit, or None to wait."""

    @abstractmethod
    def pick_replica(
        self, model_id: str, replicas: Sequence[ReplicaEndpoint]
    ) -> ReplicaEndpoint | None:
        """Choose which replica serves an interactive request."""
