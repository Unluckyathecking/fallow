"""Wire messages exchanged between agents and the coordinator.

Transport conventions (fixed for v0.1):
- All agent→coordinator communication is agent-initiated plain HTTP + JSON.
- Heartbeats every ~5s; state *transitions* are additionally pushed immediately
  as AgentEvent — routing decisions must never wait for the next heartbeat.
- Batch work is acquired by long-polling; the coordinator never dials an agent
  except to proxy inference to a replica port it learned from a heartbeat.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from fallow_protocol.base import FallowModel
from fallow_protocol.capabilities import DeviceCaps, GpuStatus, WorkerKind
from fallow_protocol.models import ReplicaStatus


class AgentState(StrEnum):
    IDLE = "idle"  # user away; fallow may use the machine
    ACTIVE = "active"  # user present; all fallow work yielded
    DRAINING = "draining"  # shutting down; finish/release leases, accept nothing


# ── Registration ─────────────────────────────────────────────────────────────


class AgentConfig(FallowModel):
    """Centrally assigned agent behaviour. Returned at registration and
    refreshable via heartbeat responses."""

    heartbeat_interval_s: float = 5.0
    idle_threshold_s: float = 120.0  # user considered away after this
    poll_interval_ms: int = 100  # idle-detector poll period
    vram_evict_after_s: float = 60.0  # suspended GPU replica → kill after this
    bench_mode: bool = False  # enables /debug/simulate_input
    assigned_models: tuple[str, ...] = ()  # model_ids this agent should serve


class RegisterRequest(FallowModel):
    enrollment_token: str
    protocol_version: int
    caps: DeviceCaps


class RegisterResponse(FallowModel):
    agent_id: str
    device_token: str
    config: AgentConfig


# ── Heartbeat ────────────────────────────────────────────────────────────────


class Heartbeat(FallowModel):
    agent_id: str
    seq: int = Field(ge=0)
    sent_at: datetime
    protocol_version: int
    state: AgentState
    user_idle_s: float = Field(ge=0)
    cpu_percent: float = Field(ge=0, le=100)
    mem_available_mb: int = Field(ge=0)
    load_avg: float | None = None
    temp_cpu_c: float | None = None
    gpus: tuple[GpuStatus, ...] = ()
    replicas: tuple[ReplicaStatus, ...] = ()
    lease_ids: tuple[str, ...] = ()  # work-unit leases currently held


class HeartbeatResponse(FallowModel):
    desired_models: tuple[str, ...] = ()  # agent reconciles replicas toward this
    revoked_lease_ids: tuple[str, ...] = ()  # stop work on these immediately
    config: AgentConfig | None = None  # present only when config changed


# ── Events (pushed immediately on state transitions) ────────────────────────


class EventKind(StrEnum):
    USER_RETURNED = "user_returned"
    USER_IDLE = "user_idle"
    REPLICA_READY = "replica_ready"
    REPLICA_SUSPENDED = "replica_suspended"
    REPLICA_RESUMED = "replica_resumed"
    REPLICA_STOPPED = "replica_stopped"
    AGENT_STARTING = "agent_starting"
    AGENT_STOPPING = "agent_stopping"


class AgentEvent(FallowModel):
    agent_id: str
    kind: EventKind
    at: datetime
    detail: dict[str, str] = Field(default_factory=dict)


# ── Batch jobs and work units ────────────────────────────────────────────────


class JobState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"  # all units done (some may be dead)
    CANCELLED = "cancelled"


class WorkUnitState(StrEnum):
    PENDING = "pending"
    LEASED = "leased"
    DONE = "done"
    DEAD = "dead"  # exhausted retries; job continues without it


class JobSubmit(FallowModel):
    """Client request to run a batch job. The coordinator splits it into
    content-addressed work units at submit time."""

    kind: WorkerKind
    model_id: str
    payload_ref: str  # coordinator-local reference to the input corpus
    params: dict[str, str] = Field(default_factory=dict)
    priority: int = 0


class WorkUnitSpec(FallowModel):
    """One independent, idempotent unit produced by splitting a job.

    work_unit_id MUST be content-derived:
    sha256(job_id ‖ idx ‖ input_hash) — duplicate completions are then
    harmless by construction and re-submits dedup for free.
    """

    work_unit_id: str
    idx: int = Field(ge=0)
    input_ref: str  # coordinator-local reference to this unit's input
    est_duration_s: float | None = None


class WorkUnitLease(FallowModel):
    """A unit handed to an agent, with an expiry after which it is requeued."""

    work_unit_id: str
    job_id: str
    kind: WorkerKind
    model_id: str
    input_url: str  # URL the agent fetches input from (coordinator-served)
    lease_expires: datetime
    attempt: int = Field(ge=1)
    est_duration_s: float | None = None


class WorkResultStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class WorkMetrics(FallowModel):
    duration_s: float = Field(ge=0)
    items: int | None = None  # chunks embedded, pages OCRed, …
    tokens: int | None = None


class WorkResult(FallowModel):
    work_unit_id: str
    status: WorkResultStatus
    result_ref: str | None = None  # set on success (uploaded separately)
    error: str | None = None  # set on failure
    metrics: WorkMetrics | None = None


class JobStatus(FallowModel):
    job_id: str
    state: JobState
    total_units: int
    done_units: int
    dead_units: int


# ── Coordinator-side views (contract for SchedulerPolicy / gateway) ─────────


class AgentSnapshot(FallowModel):
    """The coordinator's current view of one agent, as consumed by scheduling
    and routing. Built from registration caps + latest heartbeat."""

    agent_id: str
    host: str  # address the coordinator reaches replicas at (tailnet IP)
    state: AgentState
    suspect: bool  # heartbeat stale (>15s) but not yet offline
    caps: DeviceCaps
    mem_available_mb: int
    gpus: tuple[GpuStatus, ...] = ()
    replicas: tuple[ReplicaStatus, ...] = ()
    user_idle_s: float = 0.0


class ReplicaEndpoint(FallowModel):
    """A routable inference replica, as consumed by the gateway."""

    agent_id: str
    host: str
    port: int
    model_id: str
    inflight: int = 0
