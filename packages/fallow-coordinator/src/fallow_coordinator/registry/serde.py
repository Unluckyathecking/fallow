"""JSON (de)serialisation for the columns that hold protocol structures.

Uses pydantic ``TypeAdapter`` so the stored JSON always matches the wire schema
exactly (and drifts loudly if the protocol changes).
"""

from pydantic import TypeAdapter

from fallow_protocol.capabilities import DeviceCaps, GpuStatus
from fallow_protocol.models import ReplicaStatus

_CAPS = TypeAdapter(DeviceCaps)
_GPUS: TypeAdapter[tuple[GpuStatus, ...]] = TypeAdapter(tuple[GpuStatus, ...])
_REPLICAS: TypeAdapter[tuple[ReplicaStatus, ...]] = TypeAdapter(tuple[ReplicaStatus, ...])


def dump_caps(caps: DeviceCaps) -> str:
    return _CAPS.dump_json(caps).decode("utf-8")


def load_caps(raw: str) -> DeviceCaps:
    return _CAPS.validate_json(raw)


def dump_gpus(gpus: tuple[GpuStatus, ...]) -> str:
    return _GPUS.dump_json(gpus).decode("utf-8")


def load_gpus(raw: str) -> tuple[GpuStatus, ...]:
    return _GPUS.validate_json(raw)


def dump_replicas(replicas: tuple[ReplicaStatus, ...]) -> str:
    return _REPLICAS.dump_json(replicas).decode("utf-8")


def load_replicas(raw: str) -> tuple[ReplicaStatus, ...]:
    return _REPLICAS.validate_json(raw)
