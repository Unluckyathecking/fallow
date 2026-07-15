"""Coordinator-backed result payload uploads for E1.2."""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest
from main_helpers import lease

from fallow_agent.main.result_upload import ResultUploadDigestMismatch, ResultUploader
from fallow_agent.main.runner_wiring import make_coordinator_upload
from fallow_agent.workers import DeferredUploadError

BASE = "http://coordinator.test"
TOKEN = "dev-token"
AGENT_ID = "agent-1"


async def test_upload_posts_bytes_and_returns_verified_result_ref() -> None:
    payload = b"result payload"
    digest = hashlib.sha256(payload).hexdigest()
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("Authorization")
        seen["attempt"] = request.headers.get("X-Fallow-Lease-Attempt")
        seen["body"] = request.read()
        return httpx.Response(200, json={"result_ref": digest})

    uploader = ResultUploader(
        base_url=BASE,
        agent_id=AGENT_ID,
        device_token=TOKEN,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    result_ref = await uploader.upload(lease(), payload)

    assert result_ref == digest
    assert seen == {
        "path": f"/v1/agents/{AGENT_ID}/work_units/unit-1/payload",
        "auth": f"Bearer {TOKEN}",
        "attempt": "1",
        "body": payload,
    }


async def test_upload_rejects_a_returned_digest_mismatch() -> None:
    payload = b"result payload"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result_ref": "0" * 64})

    uploader = ResultUploader(
        base_url=BASE,
        agent_id=AGENT_ID,
        device_token=TOKEN,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(ResultUploadDigestMismatch, match="digest mismatch"):
        await uploader.upload(lease(), payload)


async def test_deferred_upload_preserves_payload_locally(tmp_path: Path) -> None:
    payload = b"keep this payload"

    class MismatchingUploader:
        async def upload(self, _lease: object, _payload: bytes) -> str:
            raise ResultUploadDigestMismatch("coordinator returned the wrong digest")

    upload = make_coordinator_upload(MismatchingUploader(), tmp_path)

    with pytest.raises(DeferredUploadError) as raised:
        await upload(lease(), payload)

    assert raised.value.payload_path.read_bytes() == payload


async def test_transport_failure_is_deferred_and_preserved(tmp_path: Path) -> None:
    payload = b"retry after the lease expires"

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("coordinator offline")

    uploader = ResultUploader(
        base_url=BASE,
        agent_id=AGENT_ID,
        device_token=TOKEN,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    upload = make_coordinator_upload(uploader, tmp_path)

    with pytest.raises(DeferredUploadError) as raised:
        await upload(lease(), payload)

    assert raised.value.payload_path == tmp_path / "unit-1.1.bin"
    assert raised.value.payload_path.read_bytes() == payload


async def test_rejected_upload_is_deferred_and_preserved(tmp_path: Path) -> None:
    payload = b"too large for this coordinator"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(413, json={"detail": "payload too large"})

    uploader = ResultUploader(
        base_url=BASE,
        agent_id=AGENT_ID,
        device_token=TOKEN,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    upload = make_coordinator_upload(uploader, tmp_path)

    with pytest.raises(DeferredUploadError) as raised:
        await upload(lease(), payload)

    assert raised.value.payload_path.read_bytes() == payload


async def test_malformed_upload_response_is_deferred_and_preserved(tmp_path: Path) -> None:
    payload = b"coordinator sent malformed JSON"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{not-json")

    uploader = ResultUploader(
        base_url=BASE,
        agent_id=AGENT_ID,
        device_token=TOKEN,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    upload = make_coordinator_upload(uploader, tmp_path)

    with pytest.raises(DeferredUploadError) as raised:
        await upload(lease(), payload)

    assert raised.value.payload_path.read_bytes() == payload


async def test_local_persistence_failure_is_deferred_without_posting(tmp_path: Path) -> None:
    results_path = tmp_path / "not-a-directory"
    results_path.write_bytes(b"occupied")
    posted = False

    class RecordingUploader:
        async def upload(self, _lease: object, _payload: bytes) -> str:
            nonlocal posted
            posted = True
            return "unused"

    upload = make_coordinator_upload(RecordingUploader(), results_path)

    with pytest.raises(DeferredUploadError) as raised:
        await upload(lease(), b"must not be completed")

    assert raised.value.payload_path == results_path / "unit-1.1.bin"
    assert isinstance(raised.value.cause, OSError)
    assert posted is False


async def test_verified_upload_removes_local_retry_copy(tmp_path: Path) -> None:
    payload = b"accepted payload"
    digest = hashlib.sha256(payload).hexdigest()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result_ref": digest})

    uploader = ResultUploader(
        base_url=BASE,
        agent_id=AGENT_ID,
        device_token=TOKEN,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    upload = make_coordinator_upload(uploader, tmp_path)

    assert await upload(lease(), payload) == digest
    assert not (tmp_path / "unit-1.1.bin").exists()
