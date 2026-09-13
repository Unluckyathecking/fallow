"""``MeshModelStore``: mesh-first fetch, blob fallback on any mesh failure."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
from mesh_helpers import (
    KEY,
    FakePeer,
    FakeSession,
    FakeTransport,
    RaisingTransport,
    chunks_of,
    signed_payload,
    write_blob,
)

from fallow_agent.mesh import MeshError, MeshModelStore
from fallow_agent.modelcache import HttpModelStore
from fallow_agent.modelcache.paths import blob_path, marker_path, write_marker_atomic
from fallow_protocol.models import ModelManifest

_CAP = 1 << 20
_BASE_URL = "http://coord.test"
_TOKEN = "device-token"


def _model_manifest(sha256: str, size: int) -> ModelManifest:
    return ModelManifest(
        model_id="qwen",
        family="qwen2.5",
        quant="Q4_K_M",
        file_name="qwen.gguf",
        sha256=sha256,
        size_bytes=size,
    )


def _inner(tmp_path: Path, handler: Callable[[httpx.Request], httpx.Response]) -> HttpModelStore:
    async def _no_sleep(_s: float) -> None:
        return None

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HttpModelStore(
        base_url=_BASE_URL,
        device_token=_TOKEN,
        client=client,
        cache_dir=tmp_path,
        sleep=_no_sleep,  # type: ignore[arg-type]
    )


def _blob_handler(model_id: str, data: bytes) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/v1/models/{model_id}/blob":
            return httpx.Response(200, content=data)
        return httpx.Response(404)

    return handler


def _forbid_blob(request: httpx.Request) -> httpx.Response:
    raise AssertionError("blob endpoint must not be hit when the mesh succeeds")


async def test_mesh_success_reconstructs_and_publishes(tmp_path: Path) -> None:
    data = bytes((i * 17) % 256 for i in range(2500))
    mesh_manifest = write_blob(tmp_path / "src.gguf", data)
    manifest = _model_manifest(mesh_manifest.whole_file_sha256, len(data))
    session = FakeSession(signed_payload(mesh_manifest), [FakePeer(chunks_of(data))])
    cache = tmp_path / "cache"

    store = MeshModelStore(
        inner=_inner(cache, _forbid_blob),
        transport=FakeTransport(session),
        signing_key=KEY,
        cache_dir=cache,
        store_capacity_bytes=_CAP,
    )

    path = await store.ensure(manifest)
    assert path.read_bytes() == data
    assert store.path_if_present(manifest) == path  # marker published like a blob


async def test_falls_back_to_blob_when_manifest_is_tampered(tmp_path: Path) -> None:
    data = b"w" * 2500
    mesh_manifest = write_blob(tmp_path / "src.gguf", data)
    manifest = _model_manifest(mesh_manifest.whole_file_sha256, len(data))
    payload = signed_payload(mesh_manifest)
    payload["signature"] = "0" * len(payload["signature"])  # forged
    transport = FakeTransport(FakeSession(payload, [FakePeer(chunks_of(data))]))
    cache = tmp_path / "cache"

    store = MeshModelStore(
        inner=_inner(cache, _blob_handler("qwen", data)),
        transport=transport,
        signing_key=KEY,
        cache_dir=cache,
        store_capacity_bytes=_CAP,
    )

    path = await store.ensure(manifest)
    assert path.read_bytes() == data  # served by the blob fallback


async def test_falls_back_when_the_coordinator_is_unreachable(tmp_path: Path) -> None:
    data = b"u" * 2500
    mesh_manifest = write_blob(tmp_path / "src.gguf", data)
    manifest = _model_manifest(mesh_manifest.whole_file_sha256, len(data))
    transport = RaisingTransport(httpx.ConnectError("no route"))
    cache = tmp_path / "cache"

    store = MeshModelStore(
        inner=_inner(cache, _blob_handler("qwen", data)),
        transport=transport,
        signing_key=KEY,
        cache_dir=cache,
        store_capacity_bytes=_CAP,
    )

    path = await store.ensure(manifest)
    assert path.read_bytes() == data


async def test_present_model_skips_both_paths(tmp_path: Path) -> None:
    data = b"p" * 2500
    mesh_manifest = write_blob(tmp_path / "src.gguf", data)
    manifest = _model_manifest(mesh_manifest.whole_file_sha256, len(data))
    cache = tmp_path / "cache"

    # Pre-publish the blob as an earlier fetch would; a present model must not
    # touch the mesh transport or the blob endpoint.
    dest = blob_path(cache, manifest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    write_marker_atomic(marker_path(cache, manifest), manifest.sha256)

    store = MeshModelStore(
        inner=_inner(cache, _forbid_blob),
        transport=RaisingTransport(MeshError("present model must not fetch")),
        signing_key=KEY,
        cache_dir=cache,
        store_capacity_bytes=_CAP,
    )

    assert await store.ensure(manifest) == dest
