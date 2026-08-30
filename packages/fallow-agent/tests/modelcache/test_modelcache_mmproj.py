"""mmproj companion blob: downloaded, verified, and cached beside the main blob."""

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from modelcache_helpers import MODEL_ID, blob_handler, sha256_hex

from fallow_agent.modelcache.errors import ModelVerificationError
from fallow_protocol.models import ModelManifest

_MMPROJ_NAME = "mmproj.gguf"


def _with_mmproj(manifest: ModelManifest, body: bytes, *, sha256: str | None = None):
    return manifest.model_copy(
        update={
            "mmproj_file_name": _MMPROJ_NAME,
            "mmproj_sha256": sha256 if sha256 is not None else sha256_hex(body),
            "mmproj_size_bytes": len(body),
        }
    )


def _dual_handler(
    main_body: bytes, mmproj_body: bytes, requests: list[httpx.Request] | None = None
) -> Callable[[httpx.Request], httpx.Response]:
    main = blob_handler(main_body)
    mmproj = blob_handler(mmproj_body)

    def _handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        if request.url.path.endswith("/mmproj"):
            return mmproj(request)
        return main(request)

    return _handler


async def test_ensure_downloads_and_verifies_mmproj(make_store, make_manifest, tmp_path):
    main_body, mmproj_body = b"main" * 1000, b"proj" * 500
    manifest = _with_mmproj(make_manifest(main_body), mmproj_body)
    store = make_store(_dual_handler(main_body, mmproj_body))

    path = await store.ensure(manifest)

    assert path.read_bytes() == main_body
    mmproj = tmp_path / MODEL_ID / _MMPROJ_NAME
    assert mmproj.read_bytes() == mmproj_body
    assert (tmp_path / MODEL_ID / f"{_MMPROJ_NAME}.sha256").read_text() == sha256_hex(mmproj_body)
    assert store.path_if_present(manifest) == path


async def test_mmproj_hash_mismatch_raises(make_store, make_manifest):
    main_body, mmproj_body = b"main" * 1000, b"proj" * 500
    manifest = _with_mmproj(make_manifest(main_body), mmproj_body, sha256="f" * 64)
    store = make_store(_dual_handler(main_body, mmproj_body))

    with pytest.raises(ModelVerificationError):
        await store.ensure(manifest)
    assert store.path_if_present(manifest) is None


async def test_cached_mmproj_is_not_refetched(make_store, make_manifest):
    main_body, mmproj_body = b"main" * 1000, b"proj" * 500
    manifest = _with_mmproj(make_manifest(main_body), mmproj_body)
    requests: list[httpx.Request] = []
    store = make_store(_dual_handler(main_body, mmproj_body, requests))

    first = await store.ensure(manifest)
    second = await store.ensure(manifest)

    assert first == second
    assert len(requests) == 2  # one blob fetch + one mmproj fetch, ever


async def test_missing_mmproj_refetched_when_main_cached(make_store, make_manifest, tmp_path: Path):
    main_body, mmproj_body = b"main" * 1000, b"proj" * 500
    manifest = _with_mmproj(make_manifest(main_body), mmproj_body)
    requests: list[httpx.Request] = []
    store = make_store(_dual_handler(main_body, mmproj_body, requests))

    await store.ensure(manifest)
    (tmp_path / MODEL_ID / _MMPROJ_NAME).unlink()
    (tmp_path / MODEL_ID / f"{_MMPROJ_NAME}.sha256").unlink()

    assert store.path_if_present(manifest) is None
    path = await store.ensure(manifest)

    assert path.read_bytes() == main_body
    assert (tmp_path / MODEL_ID / _MMPROJ_NAME).read_bytes() == mmproj_body
    # main blob fetched once, mmproj fetched twice
    assert sum(r.url.path.endswith("/blob") for r in requests) == 1
    assert sum(r.url.path.endswith("/mmproj") for r in requests) == 2


async def test_manifest_without_mmproj_only_fetches_blob(make_store, make_manifest):
    body = b"solo" * 1000
    manifest = make_manifest(body)
    requests: list[httpx.Request] = []
    store = make_store(_dual_handler(body, b"unused", requests))

    await store.ensure(manifest)

    assert all(r.url.path.endswith("/blob") for r in requests)
