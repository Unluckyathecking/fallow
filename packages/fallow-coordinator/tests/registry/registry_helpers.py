"""Deterministic fakes and builders for registry tests (no network, no clock)."""

from datetime import UTC, datetime, timedelta

from fallow_protocol.capabilities import DeviceCaps, OsFamily
from fallow_protocol.messages import AgentState, Heartbeat, RegisterRequest
from fallow_protocol.models import ModelManifest, ReplicaState, ReplicaStatus
from fallow_protocol.version import PROTOCOL_VERSION

START = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
SHA_ZERO = "0" * 64
ADMIN_KEY = "admin-secret-key"


class FakeClock:
    """A hand-cranked monotonic-ish wall clock injected as ``now``."""

    def __init__(self, start: datetime = START) -> None:
        self._t = start

    def __call__(self) -> datetime:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t = self._t + timedelta(seconds=seconds)


def make_caps(hostname: str = "pc1") -> DeviceCaps:
    return DeviceCaps(
        hostname=hostname,
        os=OsFamily.LINUX,
        os_version="6.1",
        cpu_model="test-cpu",
        cpu_cores=8,
        ram_mb=16384,
        disk_free_mb=100000,
        agent_version="0.1.0",
    )


def make_register_request(
    token: str,
    hostname: str = "pc1",
    protocol_version: int = PROTOCOL_VERSION,
) -> RegisterRequest:
    return RegisterRequest(
        enrollment_token=token,
        protocol_version=protocol_version,
        caps=make_caps(hostname),
    )


def make_heartbeat(
    agent_id: str,
    state: AgentState = AgentState.IDLE,
    replicas: tuple[ReplicaStatus, ...] = (),
) -> Heartbeat:
    return Heartbeat(
        agent_id=agent_id,
        seq=1,
        sent_at=START,
        protocol_version=PROTOCOL_VERSION,
        state=state,
        user_idle_s=300.0,
        cpu_percent=5.0,
        mem_available_mb=8192,
        replicas=replicas,
    )


def make_replica(model_id: str, state: ReplicaState = ReplicaState.READY) -> ReplicaStatus:
    return ReplicaStatus(model_id=model_id, port=8080, state=state, inflight=0)


def make_manifest(model_id: str = "qwen2.5-7b") -> ModelManifest:
    return ModelManifest(
        model_id=model_id,
        family="qwen2.5",
        quant="Q4_K_M",
        file_name=f"{model_id}.gguf",
        sha256=SHA_ZERO,
        size_bytes=1024,
    )
