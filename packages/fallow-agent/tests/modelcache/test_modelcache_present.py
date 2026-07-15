"""path_if_present trust-boundary and single-download concurrency tests."""

import asyncio

from conftest import FILE_NAME, MODEL_ID, blob_handler


async def test_path_if_present_none_when_absent(make_store, make_manifest):
    body = b"nope"
    manifest = make_manifest(body)
    store = make_store(blob_handler(body))

    assert store.path_if_present(manifest) is None


async def test_path_if_present_trusts_marker_without_rehash(make_store, make_manifest):
    body = b"trusted-" * 300
    manifest = make_manifest(body)
    store = make_store(blob_handler(body))
    path = await store.ensure(manifest)

    # Corrupt the blob AFTER verification, leaving the marker intact. The store
    # trusts the marker and does NOT rehash the (now wrong) bytes.
    path.write_bytes(b"CORRUPTED")

    assert store.path_if_present(manifest) == path


async def test_path_if_present_none_when_marker_missing(make_store, make_manifest, tmp_path):
    body = b"has-marker-" * 50
    manifest = make_manifest(body)
    store = make_store(blob_handler(body))
    await store.ensure(manifest)

    (tmp_path / MODEL_ID / f"{FILE_NAME}.sha256").unlink()

    assert store.path_if_present(manifest) is None


async def test_path_if_present_none_on_marker_mismatch(make_store, make_manifest):
    body = b"real-body-" * 50
    manifest = make_manifest(body)
    store = make_store(blob_handler(body))
    await store.ensure(manifest)

    # Same file on disk, but a manifest that expects a different digest.
    other = make_manifest(body, sha256="a" * 64)
    assert store.path_if_present(other) is None


async def test_concurrent_ensure_downloads_once(make_store, make_manifest):
    body = b"concurrent-" * 500
    manifest = make_manifest(body)
    requests: list = []
    store = make_store(blob_handler(body, requests=requests))

    first, second = await asyncio.gather(store.ensure(manifest), store.ensure(manifest))

    assert first == second
    assert len(requests) == 1  # the per-model lock collapsed the second fetch
