"""The mesh router: signed manifest as root of trust, per-chunk bytes, auth.

Uses the same in-memory ``FakeBlobRegistry`` and temp blob as the blob-serving
tests. No network, no real model. The coordinator signs; these tests verify with
the modelmesh public API exactly as an agent would.
"""

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport
from modelserve_helpers import BLOB_BYTES, VALID_TOKEN, FakeBlobRegistry, make_manifest

from fallow_coordinator.modelserve import MeshManifestBuilder, create_mesh_router
from fallow_coordinator.registry import ModelRecord
from fallow_modelmesh import Manifest, chunk_hash, verify_manifest

_KEY = b"shared-mesh-signing-key"
_CHUNK_SIZE = 1024  # BLOB_BYTES is 3000 bytes -> 3 chunks, exercises the chunk map.
_AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}


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
            manifest=make_manifest("disabled-model"), blob_path=str(blob_path), enabled=False
        ),
    }
    application = FastAPI()
    builder = MeshManifestBuilder(_KEY, chunk_size=_CHUNK_SIZE)
    application.include_router(create_mesh_router(FakeBlobRegistry(models), builder))
    return application


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://serve") as client:
        yield client


def _rebuild(payload: dict[str, object]) -> Manifest:
    raw = payload["manifest"]
    assert isinstance(raw, dict)
    return Manifest(
        model_id=str(raw["model_id"]),
        total_size=int(raw["total_size"]),  # type: ignore[arg-type]
        chunk_size=int(raw["chunk_size"]),  # type: ignore[arg-type]
        whole_file_sha256=str(raw["whole_file_sha256"]),
        chunks=tuple(str(c) for c in raw["chunks"]),  # type: ignore[union-attr]
        merkle_root=str(raw["merkle_root"]),
    )


async def test_manifest_is_signed_and_verifies(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/models/qwen2.5-7b/mesh/manifest", headers=_AUTH)
    assert response.status_code == 200
    payload = json.loads(response.content)
    manifest = _rebuild(payload)
    assert manifest.total_size == len(BLOB_BYTES)
    assert len(manifest.chunks) == 3  # 1024 + 1024 + 952
    assert verify_manifest(manifest, payload["signature"], _KEY)
    assert not verify_manifest(manifest, payload["signature"], b"wrong-key")


async def test_chunk_bytes_match_their_hash(client: httpx.AsyncClient) -> None:
    manifest_resp = await client.get("/v1/models/qwen2.5-7b/mesh/manifest", headers=_AUTH)
    manifest = _rebuild(json.loads(manifest_resp.content))
    rebuilt = b""
    for expected in manifest.chunks:
        resp = await client.get(f"/v1/models/qwen2.5-7b/mesh/chunk/{expected}", headers=_AUTH)
        assert resp.status_code == 200
        assert chunk_hash(resp.content) == expected
        rebuilt += resp.content
    assert rebuilt == BLOB_BYTES


async def test_manifest_requires_a_device_token(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/models/qwen2.5-7b/mesh/manifest")).status_code == 401


async def test_chunk_requires_a_device_token(client: httpx.AsyncClient) -> None:
    resp = await client.get("/v1/models/qwen2.5-7b/mesh/chunk/" + ("0" * 64))
    assert resp.status_code == 401


async def test_unknown_model_manifest_is_404(client: httpx.AsyncClient) -> None:
    resp = await client.get("/v1/models/nope/mesh/manifest", headers=_AUTH)
    assert resp.status_code == 404


async def test_disabled_model_manifest_is_404(client: httpx.AsyncClient) -> None:
    resp = await client.get("/v1/models/disabled-model/mesh/manifest", headers=_AUTH)
    assert resp.status_code == 404


async def test_unknown_chunk_is_404(client: httpx.AsyncClient) -> None:
    resp = await client.get("/v1/models/qwen2.5-7b/mesh/chunk/" + ("a" * 64), headers=_AUTH)
    assert resp.status_code == 404
