"""Fixtures for modelserve tests: a temp blob file and an ASGI client."""

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport
from modelserve_helpers import BLOB_BYTES, FakeBlobRegistry, make_manifest

from fallow_coordinator.modelserve import create_modelserve_router
from fallow_coordinator.registry import ModelRecord


@pytest.fixture
def blob_path(tmp_path: Path) -> Path:
    path = tmp_path / "qwen2.5-7b.gguf"
    path.write_bytes(BLOB_BYTES)
    return path


@pytest.fixture
def app(blob_path: Path) -> FastAPI:
    models = {
        "qwen2.5-7b": ModelRecord(
            manifest=make_manifest("qwen2.5-7b"), blob_path=str(blob_path), enabled=True
        ),
        "disabled-model": ModelRecord(
            manifest=make_manifest("disabled-model"),
            blob_path=str(blob_path),
            enabled=False,
        ),
    }
    application = FastAPI()
    application.include_router(create_modelserve_router(FakeBlobRegistry(models)))
    return application


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://serve") as client:
        yield client
