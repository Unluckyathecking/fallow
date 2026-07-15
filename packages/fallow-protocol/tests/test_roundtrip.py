"""Round-trip serialization tests for every wire type."""

from datetime import UTC, datetime

from fallow_protocol import (
    WIRE_TYPES,
    AgentEvent,
    AgentState,
    DeviceCaps,
    EventKind,
    GpuInfo,
    GpuStatus,
    Heartbeat,
    HeartbeatResponse,
    ModelManifest,
    OsFamily,
    RegisterRequest,
    ReplicaState,
    ReplicaStatus,
    WorkerKind,
    WorkResult,
    WorkResultStatus,
    WorkUnitLease,
)

CAPS = DeviceCaps(
    hostname="lab-pc-01",
    os=OsFamily.WINDOWS,
    os_version="10.0.22631",
    cpu_model="AMD Ryzen 7 5800X",
    cpu_cores=8,
    ram_mb=32768,
    disk_free_mb=250_000,
    gpus=(GpuInfo(index=0, name="NVIDIA GeForce RTX 3070", vendor="nvidia", vram_mb=8192),),
    agent_version="0.1.0",
)

SAMPLES = [
    RegisterRequest(enrollment_token="tok", protocol_version=1, caps=CAPS),
    Heartbeat(
        agent_id="a1",
        seq=42,
        sent_at=datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC),
        protocol_version=1,
        state=AgentState.IDLE,
        user_idle_s=300.5,
        cpu_percent=3.2,
        mem_available_mb=20000,
        gpus=(GpuStatus(index=0, vram_free_mb=3000, util_percent=85.0, power_w=190.0),),
        replicas=(ReplicaStatus(model_id="qwen2.5-7b", port=8801, state=ReplicaState.READY),),
        lease_ids=("u1", "u2"),
    ),
    HeartbeatResponse(desired_models=("qwen2.5-7b",), revoked_lease_ids=("u9",)),
    AgentEvent(
        agent_id="a1",
        kind=EventKind.USER_RETURNED,
        at=datetime(2026, 7, 15, 12, 0, 1, tzinfo=UTC),
        detail={"yield_ms": "112"},
    ),
    ModelManifest(
        model_id="qwen2.5-7b-instruct-q4km",
        family="qwen2.5",
        quant="Q4_K_M",
        file_name="qwen2.5-7b-instruct-q4_k_m.gguf",
        sha256="a" * 64,
        size_bytes=4_700_000_000,
        min_ram_mb=6000,
        min_vram_mb=5500,
        default_args=("--flash-attn",),
    ),
    WorkUnitLease(
        work_unit_id="w1",
        job_id="j1",
        kind=WorkerKind.EMBED,
        model_id="bge-small",
        input_url="http://coordinator:8000/v1/work_units/w1/input",
        lease_expires=datetime(2026, 7, 15, 12, 10, 0, tzinfo=UTC),
        attempt=1,
    ),
    WorkResult(work_unit_id="w1", status=WorkResultStatus.SUCCEEDED, result_ref="r1"),
]


def test_samples_cover_key_types():
    covered = {type(s) for s in SAMPLES}
    key = {
        RegisterRequest,
        Heartbeat,
        HeartbeatResponse,
        AgentEvent,
        ModelManifest,
        WorkUnitLease,
        WorkResult,
    }
    assert key <= covered


def test_json_roundtrip():
    for sample in SAMPLES:
        raw = sample.model_dump_json()
        back = type(sample).model_validate_json(raw)
        assert back == sample, f"round-trip mismatch for {type(sample).__name__}"


def test_all_wire_types_are_frozen_and_strict():
    for model in WIRE_TYPES:
        assert model.model_config.get("frozen"), f"{model.__name__} must be frozen"
        assert model.model_config.get("extra") == "forbid", f"{model.__name__} must forbid extras"
