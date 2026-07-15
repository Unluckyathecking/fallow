"""Download-path tests: full fetch, resume, and Range-ignored restart."""

from pathlib import Path

from conftest import FILE_NAME, MODEL_ID, blob_handler, sha256_hex


async def test_full_download_writes_verified_blob(make_store, make_manifest, tmp_path):
    body = b"gguf-bytes-" * 5000
    manifest = make_manifest(body)
    store = make_store(blob_handler(body))

    path = await store.ensure(manifest)

    assert path == tmp_path / MODEL_ID / FILE_NAME
    assert path.read_bytes() == body
    marker = tmp_path / MODEL_ID / f"{FILE_NAME}.sha256"
    assert marker.read_text() == sha256_hex(body)
    assert not (tmp_path / MODEL_ID / f"{FILE_NAME}.part").exists()
    assert store.path_if_present(manifest) == path


async def test_ensure_returns_cached_path_without_second_download(make_store, make_manifest):
    body = b"cached" * 1000
    manifest = make_manifest(body)
    requests: list = []
    store = make_store(blob_handler(body, requests=requests))

    first = await store.ensure(manifest)
    second = await store.ensure(manifest)

    assert first == second
    assert len(requests) == 1


async def test_resume_sends_range_and_appends(make_store, make_manifest, tmp_path):
    body = bytes(range(256)) * 4000  # 1,024,000 bytes: spans several 1 MiB chunks
    manifest = make_manifest(body)
    prefix_len = 1000
    model_dir: Path = tmp_path / MODEL_ID
    model_dir.mkdir(parents=True)
    (model_dir / f"{FILE_NAME}.part").write_bytes(body[:prefix_len])
    requests: list = []
    store = make_store(blob_handler(body, requests=requests))

    path = await store.ensure(manifest)

    assert path.read_bytes() == body
    assert requests[0].headers.get("Range") == f"bytes={prefix_len}-"


async def test_range_ignored_restarts_from_zero(make_store, make_manifest, tmp_path):
    body = b"complete-body-" * 2000
    manifest = make_manifest(body)
    model_dir: Path = tmp_path / MODEL_ID
    model_dir.mkdir(parents=True)
    # A stale partial that is NOT a prefix of body; a 200 response must discard it.
    (model_dir / f"{FILE_NAME}.part").write_bytes(b"STALE-PARTIAL-DATA")
    store = make_store(blob_handler(body, ignore_range=True))

    path = await store.ensure(manifest)

    assert path.read_bytes() == body
