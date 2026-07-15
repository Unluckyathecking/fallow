"""Fixtures for model-cache tests.

Non-fixture constants and upstream scripts live in ``modelcache_helpers``;
conftest is fixtures-only. Everything is in-process: an ``httpx.MockTransport``
stands in for the coordinator, and the cache directory is a pytest ``tmp_path``.
No network, no real llama-server, no GPU.
"""

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from modelcache_helpers import BASE_URL, DEVICE_TOKEN, FILE_NAME, MODEL_ID, sha256_hex

from fallow_agent.modelcache import HttpModelStore, ModelCacheConfig
from fallow_protocol.capabilities import WorkerKind
from fallow_protocol.models import ModelManifest


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
