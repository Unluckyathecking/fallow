"""Blob + manifest endpoint behaviour: auth, Range resume, full download, 404s."""

import httpx
from modelserve_helpers import BLOB_BYTES, VALID_TOKEN, make_manifest

_AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}
_BLOB_URL = "/v1/models/qwen2.5-7b/blob"


async def test_blob_requires_auth(client: httpx.AsyncClient) -> None:
    assert (await client.get(_BLOB_URL)).status_code == 401


async def test_blob_rejects_bad_token(client: httpx.AsyncClient) -> None:
    response = await client.get(_BLOB_URL, headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401


async def test_full_download_matches_file(client: httpx.AsyncClient) -> None:
    response = await client.get(_BLOB_URL, headers=_AUTH)

    assert response.status_code == 200
    assert response.content == BLOB_BYTES
    assert response.headers["content-length"] == str(len(BLOB_BYTES))
    assert response.headers["accept-ranges"] == "bytes"


async def test_range_resume_returns_exact_tail(client: httpx.AsyncClient) -> None:
    start = 1000
    response = await client.get(_BLOB_URL, headers={**_AUTH, "Range": f"bytes={start}-"})

    total = len(BLOB_BYTES)
    assert response.status_code == 206
    assert response.content == BLOB_BYTES[start:]
    assert response.headers["content-length"] == str(total - start)
    assert response.headers["content-range"] == f"bytes {start}-{total - 1}/{total}"


async def test_range_closed_interval(client: httpx.AsyncClient) -> None:
    response = await client.get(_BLOB_URL, headers={**_AUTH, "Range": "bytes=10-19"})

    assert response.status_code == 206
    assert response.content == BLOB_BYTES[10:20]
    assert response.headers["content-length"] == "10"


async def test_unsatisfiable_range_returns_416(client: httpx.AsyncClient) -> None:
    response = await client.get(_BLOB_URL, headers={**_AUTH, "Range": "bytes=99999-"})
    assert response.status_code == 416


async def test_unknown_model_returns_404(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/models/ghost/blob", headers=_AUTH)
    assert response.status_code == 404


async def test_disabled_model_returns_404(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/models/disabled-model/blob", headers=_AUTH)
    assert response.status_code == 404


async def test_manifest_endpoint_returns_json(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/models/qwen2.5-7b/manifest", headers=_AUTH)

    assert response.status_code == 200
    assert response.json() == make_manifest("qwen2.5-7b").model_dump(mode="json")


async def test_manifest_requires_auth(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/models/qwen2.5-7b/manifest")).status_code == 401
