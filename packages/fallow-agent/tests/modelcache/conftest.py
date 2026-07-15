"""Shared fixtures for model-cache tests.

Everything is in-process: an ``httpx.MockTransport`` stands in for the
coordinator, and the cache directory is a pytest ``tmp_path``. No network, no
real llama-server, no GPU.
"""

import hashlib
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from fallow_agent.modelcache import HttpModelStore, ModelCacheConfig
from fallow_protocol.capabilities import WorkerKind
from fallow_protocol.models import ModelManifest

BASE_URL = "http://coordinator.test"
DEVICE_TOKEN = "tok-abc123"
MODEL_ID = "qwen2.5-7b-instruct-q4km"
FILE_NAME = "model.gguf"


def sha256_hex(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


@pytest.fixture
def make_manifest() -> Callable[..., ModelManifest]:
    def _make(
        body: bytes, *, sha256: str | None = None, size_bytes: int | None = None
    ) -> ModelManifest:
        return ModelManifest(
            model_id=MODEL_ID,
            family="qwen2.5",
            quant="Q4_K_M",
            worker_kind=WorkerKind.CHAT,
            file_name=FILE_NAME,
            sha256=sha256 if sha256 is not None else sha256_hex(body),
            size_bytes=size_bytes if size_bytes is not None else len(body),
        )

    return _make


@pytest.fixture
def make_store(tmp_path: Path) -> Callable[..., HttpModelStore]:
    async def _instant_sleep(_seconds: float) -> None:
        return None

    def _make(
        handler: Callable[[httpx.Request], httpx.Response],
        *,
        cache_dir: Path | None = None,
        config: ModelCacheConfig | None = None,
        sleep: Callable[[float], object] | None = None,
    ) -> HttpModelStore:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return HttpModelStore(
            base_url=BASE_URL,
            device_token=DEVICE_TOKEN,
            client=client,
            cache_dir=cache_dir or tmp_path,
            config=config,
            sleep=sleep or _instant_sleep,  # type: ignore[arg-type]
        )

    return _make


def blob_handler(
    body: bytes,
    *,
    ignore_range: bool = False,
    requests: list[httpx.Request] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Serve ``body`` as a coordinator blob endpoint, honouring Range."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        rng = request.headers.get("Range")
        if rng and not ignore_range:
            start = int(rng.removeprefix("bytes=").split("-", 1)[0])
            chunk = body[start:]
            headers = {"Content-Range": f"bytes {start}-{len(body) - 1}/{len(body)}"}
            return httpx.Response(206, content=chunk, headers=headers)
        return httpx.Response(200, content=body)

    return _handler
