"""Shared, offline test helpers for the fallow-cli suite.

Sample wire objects, ``httpx.MockTransport`` factories, and hand-built GGUF
headers. Imported bare (``from cli_helpers import ...``) since pytest prepends
the tests dir to sys.path. Nothing here touches a real network, blob source, or
GPU — the GGUF fixtures are assembled byte by byte rather than downloaded.
"""

from __future__ import annotations

import json
import struct

import httpx

from fallow_protocol import (
    AgentSnapshot,
    AgentState,
    DeviceCaps,
    JobState,
    JobStatus,
    ModelManifest,
    OsFamily,
    ReplicaState,
    ReplicaStatus,
    WorkerKind,
)

COORD_URL = "http://coordinator.test"
SHA_ZERO = "0" * 64
Route = tuple[int, object | None]
Routes = dict[tuple[str, str], Route]


def sample_agent() -> AgentSnapshot:
    caps = DeviceCaps(
        hostname="pc-1",
        os=OsFamily.LINUX,
        os_version="6.1",
        cpu_model="Ryzen",
        cpu_cores=8,
        ram_mb=32000,
        disk_free_mb=100000,
        agent_version="0.1.0",
    )
    replica = ReplicaStatus(model_id="qwen", port=8081, state=ReplicaState.READY)
    return AgentSnapshot(
        agent_id="agent-1",
        host="100.64.0.2",
        state=AgentState.IDLE,
        suspect=False,
        caps=caps,
        mem_available_mb=16000,
        replicas=(replica,),
        user_idle_s=42.0,
    )


def sample_manifest() -> ModelManifest:
    return ModelManifest(
        model_id="qwen",
        family="qwen2.5",
        quant="Q4_K_M",
        worker_kind=WorkerKind.CHAT,
        file_name="qwen.gguf",
        sha256=SHA_ZERO,
        size_bytes=4_000_000,
    )


def sample_job() -> JobStatus:
    return JobStatus(
        job_id="job-1", state=JobState.RUNNING, total_units=10, done_units=3, dead_units=0
    )


def make_transport(routes: Routes) -> httpx.MockTransport:
    """Answer (method, path) from ``routes``; 404 for anything else."""

    def handler(request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        if key not in routes:
            return httpx.Response(404, json={"detail": f"no route for {key}"})
        status, body = routes[key]
        if body is None:
            return httpx.Response(status)
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler)


def raising_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    return httpx.MockTransport(handler)


def recording_transport(
    store: dict[str, object], status: int = 201, response_body: object | None = None
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        store["path"] = request.url.path
        if request.content:
            store["body"] = json.loads(request.content)
        if response_body is not None:
            return httpx.Response(status, json=response_body)
        return httpx.Response(status)

    return httpx.MockTransport(handler)


def bytes_transport(payload: bytes) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload, headers={"content-length": str(len(payload))})

    return httpx.MockTransport(handler)


# ── GGUF fixtures ────────────────────────────────────────────────────────────
GGUF_STRING = 8
GGUF_ARRAY = 9
GGUF_UINT32 = 4
GGUF_FLOAT32 = 6


def gguf_string(value: str) -> bytes:
    raw = value.encode("utf-8")
    return struct.pack("<Q", len(raw)) + raw


def gguf_kv_string(key: str, value: str) -> bytes:
    return gguf_string(key) + struct.pack("<I", GGUF_STRING) + gguf_string(value)


def gguf_kv_uint32(key: str, value: int) -> bytes:
    return gguf_string(key) + struct.pack("<I", GGUF_UINT32) + struct.pack("<I", value)


def gguf_kv_float32(key: str, value: float) -> bytes:
    return gguf_string(key) + struct.pack("<I", GGUF_FLOAT32) + struct.pack("<f", value)


def gguf_kv_string_array(key: str, values: list[str]) -> bytes:
    body = b"".join(gguf_string(v) for v in values)
    head = struct.pack("<IQ", GGUF_STRING, len(values))
    return gguf_string(key) + struct.pack("<I", GGUF_ARRAY) + head + body


def gguf_file(kvs: list[bytes], *, version: int = 3, tensor_count: int = 290) -> bytes:
    return b"GGUF" + struct.pack("<IQQ", version, tensor_count, len(kvs)) + b"".join(kvs)


def sample_gguf(*, file_type: int = 15, version: int = 3) -> bytes:
    """A plausible v3 header: the three keys we read, plus noise to skip over."""
    return gguf_file(
        [
            gguf_kv_string("general.architecture", "qwen2"),
            gguf_kv_string("general.name", "Qwen2.5 0.5B Instruct"),
            gguf_kv_uint32("general.file_type", file_type),
            gguf_kv_float32("qwen2.attention.layer_norm_rms_epsilon", 1e-6),
            gguf_kv_string_array("tokenizer.ggml.tokens", ["a", "b", "c"]),
            gguf_kv_string("tokenizer.chat_template", "{{ bos }}"),
        ],
        version=version,
    )
