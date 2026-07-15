"""Verification and retry/backoff tests."""

import httpx
import pytest
from conftest import FILE_NAME, MODEL_ID, blob_handler

from fallow_agent.modelcache import (
    ModelCacheConfig,
    ModelFetchError,
    ModelVerificationError,
)


async def test_hash_mismatch_deletes_part_and_raises(make_store, make_manifest, tmp_path):
    body = b"actual-bytes-" * 100
    manifest = make_manifest(body, sha256="0" * 64)  # valid format, wrong digest
    store = make_store(blob_handler(body))

    with pytest.raises(ModelVerificationError):
        await store.ensure(manifest)

    assert not (tmp_path / MODEL_ID / f"{FILE_NAME}.part").exists()
    assert not (tmp_path / MODEL_ID / FILE_NAME).exists()


async def test_size_mismatch_raises(make_store, make_manifest):
    body = b"x" * 500
    manifest = make_manifest(body, size_bytes=999)  # sha correct, size wrong
    store = make_store(blob_handler(body))

    with pytest.raises(ModelVerificationError):
        await store.ensure(manifest)


async def test_retries_then_succeeds(make_store, make_manifest):
    body = b"payload-" * 200
    manifest = make_manifest(body)
    calls = {"n": 0}
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(503)
        return httpx.Response(200, content=body)

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    store = make_store(handler, sleep=record_sleep)
    path = await store.ensure(manifest)

    assert path.read_bytes() == body
    assert calls["n"] == 3
    assert sleeps == [0.5, 1.0]  # exponential backoff off a 0.5s base


async def test_exhausts_retries_raises_fetch_error(make_store, make_manifest):
    body = b"z" * 32
    manifest = make_manifest(body)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    store = make_store(handler, config=ModelCacheConfig(max_retries=2))

    with pytest.raises(ModelFetchError):
        await store.ensure(manifest)


async def test_transport_error_is_retried_then_fails(make_store, make_manifest):
    body = b"z" * 32
    manifest = make_manifest(body)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("boom")

    store = make_store(handler, config=ModelCacheConfig(max_retries=1))

    with pytest.raises(ModelFetchError):
        await store.ensure(manifest)
    assert calls["n"] == 2  # initial attempt + one retry
