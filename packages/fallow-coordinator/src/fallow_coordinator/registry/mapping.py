"""Pure row -> protocol-view mapping helpers.

Kept clock-free and I/O-free so they are trivially unit-testable; the store
computes ``suspect``/liveness and hands finished flags in.
"""

from collections.abc import Iterator

import aiosqlite

from fallow_coordinator.registry.serde import load_caps, load_gpus, load_replicas
from fallow_protocol.messages import AgentSnapshot, AgentState, ReplicaEndpoint
from fallow_protocol.models import ReplicaState


def _optional_float(value: float | None) -> float | None:
    """Coerce a nullable REAL column to float, preserving NULL as None."""
    return None if value is None else float(value)


def snapshot_from_row(row: aiosqlite.Row, *, suspect: bool) -> AgentSnapshot:
    """Build the coordinator's view of one agent from its stored row."""
    return AgentSnapshot(
        agent_id=str(row["agent_id"]),
        host=str(row["host"]),
        state=AgentState(row["state"]),
        suspect=suspect,
        caps=load_caps(row["caps_json"]),
        mem_available_mb=int(row["mem_available_mb"]),
        gpus=load_gpus(row["gpus_json"]),
        replicas=load_replicas(row["replicas_json"]),
        user_idle_s=float(row["user_idle_s"]),
        serving_paused=bool(row["serving_paused"]),
        predicted_idle_remaining_s=_optional_float(row["predicted_idle_remaining_s"]),
        predicted_idle_confidence=_optional_float(row["predicted_idle_confidence"]),
    )


def ready_endpoints_for_row(row: aiosqlite.Row, model_id: str) -> Iterator[ReplicaEndpoint]:
    """Yield routable endpoints for READY replicas of ``model_id`` on this row."""
    agent_id = str(row["agent_id"])
    host = str(row["host"])
    for replica in load_replicas(row["replicas_json"]):
        if replica.model_id != model_id or replica.state != ReplicaState.READY:
            continue
        yield ReplicaEndpoint(
            agent_id=agent_id,
            host=host,
            port=replica.port,
            model_id=model_id,
            inflight=replica.inflight,
        )
