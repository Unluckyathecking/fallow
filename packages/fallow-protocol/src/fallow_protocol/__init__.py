"""fallow-protocol: wire types and interface contracts for Fallow.

This package depends on pydantic + stdlib ONLY (CI-enforced). It is the
portability boundary for a future Go/Rust port: everything the coordinator
and agent share lives here, and nothing else does.
"""

from pydantic import BaseModel

from fallow_protocol.base import FallowModel
from fallow_protocol.capabilities import (
    DeviceCaps,
    GpuInfo,
    GpuStatus,
    ModelRequirements,
    OsFamily,
    WorkerKind,
)
from fallow_protocol.interfaces import (
    EventSink,
    IdleDetector,
    ModelStore,
    Preemptor,
    ProcessSupervisor,
    QueueStore,
    SchedulerPolicy,
)
from fallow_protocol.messages import (
    AgentConfig,
    AgentEvent,
    AgentSnapshot,
    AgentState,
    EventKind,
    Heartbeat,
    HeartbeatResponse,
    JobState,
    JobStatus,
    JobSubmit,
    RegisterRequest,
    RegisterResponse,
    ReplicaEndpoint,
    UnitTransition,
    WorkMetrics,
    WorkResult,
    WorkResultStatus,
    WorkUnitLease,
    WorkUnitSpec,
    WorkUnitState,
)
from fallow_protocol.models import ModelManifest, ReplicaState, ReplicaStatus
from fallow_protocol.version import PROTOCOL_VERSION, __version__

# Every concrete wire type, exported to JSON Schema by export_schemas.
WIRE_TYPES: tuple[type[BaseModel], ...] = (
    AgentConfig,
    AgentEvent,
    AgentSnapshot,
    DeviceCaps,
    GpuInfo,
    GpuStatus,
    Heartbeat,
    HeartbeatResponse,
    JobStatus,
    JobSubmit,
    ModelManifest,
    ModelRequirements,
    RegisterRequest,
    RegisterResponse,
    ReplicaEndpoint,
    ReplicaStatus,
    UnitTransition,
    WorkMetrics,
    WorkResult,
    WorkUnitLease,
    WorkUnitSpec,
)

__all__ = [
    "PROTOCOL_VERSION",
    "WIRE_TYPES",
    "AgentConfig",
    "AgentEvent",
    "AgentSnapshot",
    "AgentState",
    "DeviceCaps",
    "EventKind",
    "EventSink",
    "FallowModel",
    "GpuInfo",
    "GpuStatus",
    "Heartbeat",
    "HeartbeatResponse",
    "IdleDetector",
    "JobState",
    "JobStatus",
    "JobSubmit",
    "ModelManifest",
    "ModelRequirements",
    "ModelStore",
    "OsFamily",
    "Preemptor",
    "ProcessSupervisor",
    "QueueStore",
    "RegisterRequest",
    "RegisterResponse",
    "ReplicaEndpoint",
    "ReplicaState",
    "ReplicaStatus",
    "SchedulerPolicy",
    "UnitTransition",
    "WorkMetrics",
    "WorkResult",
    "WorkResultStatus",
    "WorkUnitLease",
    "WorkUnitSpec",
    "WorkUnitState",
    "WorkerKind",
    "__version__",
]
