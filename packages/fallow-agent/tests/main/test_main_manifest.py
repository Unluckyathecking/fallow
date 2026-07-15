"""ManifestFetcher: parse a 200 body, and error on non-200 / bad body."""

from __future__ import annotations

import httpx
import pytest
from main_helpers import manifest

from fallow_agent.main.errors import ManifestFetchError
from fallow_agent.main.manifest import ManifestFetcher

BASE = "http://coordinator.test"
TOKEN = "dev-tok"


def _fetcher(handler: object) -> ManifestFetcher:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return ManifestFetcher(base_url=BASE, device_token=TOKEN, client=client)


async def test_fetches_and_parses_manifest() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models/m1/manifest"
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        return httpx.Response(200, content=manifest("m1").model_dump_json())

    result = await _fetcher(handler).fetch("m1")
    assert result.model_id == "m1"


async def test_non_200_raises() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "unknown model"})

    with pytest.raises(ManifestFetchError, match="404"):
        await _fetcher(handler).fetch("ghost")


async def test_malformed_body_raises() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{not-json")

    with pytest.raises(ManifestFetchError, match="malformed"):
        await _fetcher(handler).fetch("m1")
