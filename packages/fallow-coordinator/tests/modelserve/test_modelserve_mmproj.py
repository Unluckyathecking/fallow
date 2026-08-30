"""mmproj endpoint: served beside the main blob, only for manifests that declare one."""

import httpx
from modelserve_helpers import MMPROJ_BYTES, VALID_TOKEN

_AUTH = {"Authorization": f"Bearer {VALID_TOKEN}"}
_MMPROJ_URL = "/v1/models/vlm-model/mmproj"


async def test_mmproj_requires_auth(client: httpx.AsyncClient) -> None:
    assert (await client.get(_MMPROJ_URL)).status_code == 401


async def test_mmproj_full_download_matches_file(client: httpx.AsyncClient) -> None:
    response = await client.get(_MMPROJ_URL, headers=_AUTH)

    assert response.status_code == 200
    assert response.content == MMPROJ_BYTES
    assert response.headers["content-length"] == str(len(MMPROJ_BYTES))
    assert response.headers["accept-ranges"] == "bytes"


async def test_mmproj_range_resume_returns_exact_tail(client: httpx.AsyncClient) -> None:
    start = 400
    response = await client.get(_MMPROJ_URL, headers={**_AUTH, "Range": f"bytes={start}-"})

    total = len(MMPROJ_BYTES)
    assert response.status_code == 206
    assert response.content == MMPROJ_BYTES[start:]
    assert response.headers["content-range"] == f"bytes {start}-{total - 1}/{total}"


async def test_mmproj_404_for_model_without_one(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/models/qwen2.5-7b/mmproj", headers=_AUTH)
    assert response.status_code == 404


async def test_mmproj_404_for_unknown_model(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/models/ghost/mmproj", headers=_AUTH)
    assert response.status_code == 404
