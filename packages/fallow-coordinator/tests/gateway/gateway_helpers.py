"""In-memory fakes and upstream scripts for gateway tests (no network, no GPU).

The gateway talks to two httpx clients in these tests: the *test* client speaks
to the FastAPI app over ``ASGITransport``; the *upstream* client speaks to a
``MockTransport`` handler that role-plays llama-server (emitting SSE bytes,
refusing connections, or truncating mid-stream).
"""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta

import httpx

from fallow_coordinator.registry import ApiKeyInfo
from fallow_protocol.capabilities import WorkerKind
from fallow_protocol.messages import ReplicaEndpoint
from fallow_protocol.models import ModelManifest

Handler = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]

CHAT_MODEL = "qwen2.5-7b"
EMBED_MODEL = "bge-small"
SHA_ZERO = "0" * 64

ADMIN_KEY = "admin-key"
RESTRICTED_KEY = "restricted-key"

DEFAULT_KEYS: dict[str, ApiKeyInfo] = {
    ADMIN_KEY: ApiKeyInfo(name="admin", model_allowlist=None, is_admin=True),
    RESTRICTED_KEY: ApiKeyInfo(name="team-a", model_allowlist=(CHAT_MODEL,)),
}


class Clock:
    """Deterministic monotonically-increasing clock (one second per call)."""

    def __init__(self) -> None:
        self._now = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)
        self._step = timedelta(seconds=1)

    def __call__(self) -> datetime:
        current = self._now
        self._now += self._step
        return current


class FakeGatewayRegistry:
    """Structural :class:`GatewayRegistry`: dict-backed keys, endpoints, models."""

    def __init__(
        self,
        api_keys: dict[str, ApiKeyInfo],
        endpoints: dict[str, tuple[ReplicaEndpoint, ...]],
        models: tuple[ModelManifest, ...],
    ) -> None:
        self._keys = api_keys
        self._endpoints = endpoints
        self._models = models

    async def authenticate_api_key(self, bearer: str) -> ApiKeyInfo | None:
        return self._keys.get(bearer)

    async def replica_endpoints(self, model_id: str, now: datetime) -> tuple[ReplicaEndpoint, ...]:
        return self._endpoints.get(model_id, ())

    async def list_models(self) -> tuple[ModelManifest, ...]:
        return self._models


class RecordingRequestLog:
    """Collects emitted :class:`GatewayLogEntry` records in memory."""

    def __init__(self) -> None:
        self.entries: list[object] = []

    def log(self, entry: object) -> None:
        self.entries.append(entry)


def make_manifest(model_id: str, kind: WorkerKind = WorkerKind.CHAT) -> ModelManifest:
    return ModelManifest(
        model_id=model_id,
        family=model_id.split("-")[0],
        quant="Q4_K_M",
        worker_kind=kind,
        file_name=f"{model_id}.gguf",
        sha256=SHA_ZERO,
        size_bytes=1024,
    )


DEFAULT_MODELS = (
    make_manifest(CHAT_MODEL, WorkerKind.CHAT),
    make_manifest(EMBED_MODEL, WorkerKind.EMBED),
)


def make_endpoint(
    host: str, port: int, model_id: str = CHAT_MODEL, agent_id: str = "agent-1"
) -> ReplicaEndpoint:
    return ReplicaEndpoint(agent_id=agent_id, host=host, port=port, model_id=model_id)


def first_pick(_model: str, replicas: Sequence[ReplicaEndpoint]) -> ReplicaEndpoint | None:
    """Trivial policy used in tests: take the first offered replica."""
    return replicas[0] if replicas else None


def buffered_handler(body: bytes, status_code: int = 200) -> Handler:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, content=body)

    return handler


def sse_handler(chunks: Sequence[bytes], delay_s: float = 0.0) -> Handler:
    async def handler(_request: httpx.Request) -> httpx.Response:
        async def gen():
            for chunk in chunks:
                if delay_s:
                    await asyncio.sleep(delay_s)
                yield chunk

        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=gen())

    return handler
