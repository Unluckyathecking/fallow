"""Shared builders and fakes for the C4 scheduler tests.

Everything here is deterministic and in-memory: no network, no SQLite, no
llama-server, no GPU. Policies are pure, so most tests are plain tables over
hand-built ``AgentSnapshot`` / ``ReplicaEndpoint`` values.
"""

from collections.abc import Sequence

from fallow_protocol.capabilities import DeviceCaps, GpuInfo, OsFamily
from fallow_protocol.interfaces import QueueStore
from fallow_protocol.messages import (
    AgentSnapshot,
    AgentState,
    JobStatus,
    JobSubmit,
    ReplicaEndpoint,
    WorkResult,
    WorkUnitLease,
    WorkUnitSpec,
)
from fallow_protocol.models import ReplicaState, ReplicaStatus


def make_caps(gpu_count: int = 0) -> DeviceCaps:
    gpus = tuple(
        GpuInfo(index=i, name=f"gpu{i}", vendor="nvidia", vram_mb=8192) for i in range(gpu_count)
    )
    return DeviceCaps(
        hostname="host",
        os=OsFamily.LINUX,
        os_version="1",
        cpu_model="cpu",
        cpu_cores=8,
        ram_mb=16384,
        disk_free_mb=100_000,
        gpus=gpus,
        agent_version="0.1.0",
    )


def make_replica_status(model_id: str, state: ReplicaState = ReplicaState.READY) -> ReplicaStatus:
    return ReplicaStatus(model_id=model_id, port=8000, state=state, inflight=0, gpu=False)


def make_agent(
    agent_id: str,
    *,
    state: AgentState = AgentState.IDLE,
    suspect: bool = False,
    gpu_count: int = 0,
    mem_available_mb: int = 1000,
    replicas: Sequence[ReplicaStatus] = (),
) -> AgentSnapshot:
    return AgentSnapshot(
        agent_id=agent_id,
        host=f"10.0.0.{len(agent_id)}",
        state=state,
        suspect=suspect,
        caps=make_caps(gpu_count),
        mem_available_mb=mem_available_mb,
        replicas=tuple(replicas),
    )


def make_endpoint(
    *, host: str, port: int, model_id: str = "m1", inflight: int = 0, agent_id: str = "a"
) -> ReplicaEndpoint:
    return ReplicaEndpoint(
        agent_id=agent_id, host=host, port=port, model_id=model_id, inflight=inflight
    )


class FakeQueue(QueueStore):
    """In-memory ``QueueStore`` whose ``requeue_expired`` is scriptable.

    Only ``requeue_expired`` is exercised by the dispatch loop; the rest satisfy
    the ABC and fail loudly if a test ever reaches them unexpectedly.
    """

    def __init__(self, requeue_return: int = 0, requeue_error: Exception | None = None) -> None:
        self.requeue_return = requeue_return
        self.requeue_error = requeue_error
        self.requeue_calls = 0

    async def requeue_expired(self) -> int:
        self.requeue_calls += 1
        if self.requeue_error is not None:
            raise self.requeue_error
        return self.requeue_return

    async def submit_job(self, job: JobSubmit, units: Sequence[WorkUnitSpec]) -> str:
        raise NotImplementedError

    async def job_status(self, job_id: str) -> JobStatus | None:
        raise NotImplementedError

    async def lease_next(self, agent_id: str, model_ids: Sequence[str]) -> WorkUnitLease | None:
        raise NotImplementedError

    async def extend_leases(self, agent_id: str, unit_ids: Sequence[str]) -> None:
        raise NotImplementedError

    async def complete_unit(self, agent_id: str, result: WorkResult) -> None:
        raise NotImplementedError

    async def requeue_agent(self, agent_id: str) -> int:
        raise NotImplementedError
