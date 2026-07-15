"""Content-addressed storage for streamed work-unit result payloads."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

import fallow_coordinator.app.result_blobs as result_blobs
from fallow_coordinator.app.result_blobs import ResultBlobStore, ResultPayloadTooLarge


async def _stream(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


async def test_store_streams_payload_to_its_sha256_path(tmp_path: Path) -> None:
    payload = b"a" * (1024 * 1024 + 17)
    expected = hashlib.sha256(payload).hexdigest()
    store = ResultBlobStore(tmp_path, max_payload_bytes=len(payload))

    digest = await store.put(_stream(payload))

    assert digest == expected
    assert (tmp_path / expected).read_bytes() == payload


async def test_store_rejects_limit_crossing_and_removes_temp_file(tmp_path: Path) -> None:
    store = ResultBlobStore(tmp_path, max_payload_bytes=5)

    with pytest.raises(ResultPayloadTooLarge) as raised:
        await store.put(_stream(b"123", b"456"))

    assert raised.value.max_payload_bytes == 5
    assert raised.value.received_payload_bytes == 6
    assert list(tmp_path.iterdir()) == []


async def test_store_propagates_stream_error_and_removes_temp_file(tmp_path: Path) -> None:
    async def broken_stream() -> AsyncIterator[bytes]:
        yield b"partial"
        raise OSError("source failed")

    store = ResultBlobStore(tmp_path, max_payload_bytes=100)

    with pytest.raises(OSError, match="source failed"):
        await store.put(broken_stream())

    assert list(tmp_path.iterdir()) == []


async def test_store_cancellation_removes_temp_file(tmp_path: Path) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def stalled_stream() -> AsyncIterator[bytes]:
        yield b"partial"
        started.set()
        await release.wait()

    store = ResultBlobStore(tmp_path, max_payload_bytes=100)
    task = asyncio.create_task(store.put(stalled_stream()))
    await started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert list(tmp_path.iterdir()) == []


async def test_concurrent_identical_writes_are_idempotent(tmp_path: Path) -> None:
    payload = b"shared result"
    expected = hashlib.sha256(payload).hexdigest()
    store = ResultBlobStore(tmp_path, max_payload_bytes=100)

    digests = await asyncio.gather(
        store.put(_stream(payload)),
        store.put(_stream(payload)),
    )

    assert digests == [expected, expected]
    assert [path.name for path in tmp_path.iterdir()] == [expected]
    assert (tmp_path / expected).read_bytes() == payload


async def test_existing_digest_is_not_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"already stored"
    expected = hashlib.sha256(payload).hexdigest()
    store = ResultBlobStore(tmp_path, max_payload_bytes=100)
    assert await store.put(_stream(payload)) == expected

    def reject_replace(source: Path, destination: Path) -> None:
        raise AssertionError(f"unexpected replacement: {source} -> {destination}")

    monkeypatch.setattr(result_blobs.os, "replace", reject_replace)

    assert await store.put(_stream(payload)) == expected
    assert (tmp_path / expected).read_bytes() == payload
