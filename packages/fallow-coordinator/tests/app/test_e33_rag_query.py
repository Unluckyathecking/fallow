from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from app_helpers import (
    ADMIN_KEY,
    FakeClock,
    bearer,
    enrolled_idle_agent,
    make_replica,
)
from httpx import ASGITransport

from fallow_coordinator.app import CoordinatorConfig, create_app
from fallow_coordinator.rag import Chunk

_MODEL = "bge-small"


def _config(tmp_path: Path) -> CoordinatorConfig:
    return CoordinatorConfig(
        db_path=tmp_path / "coordinator.db",
        blob_dir=tmp_path / "blobs",
        unit_input_dir=tmp_path / "units",
        result_dir=tmp_path / "results",
        events_jsonl_path=tmp_path / "events.jsonl",
        gateway_log_path=tmp_path / "gateway.jsonl",
        admin_key=ADMIN_KEY,
        requeue_interval_s=3600,
    )


@pytest.mark.asyncio
async def test_query_embeds_live_and_returns_the_planted_chunk_first(tmp_path: Path) -> None:
    upstream_requests: list[httpx.Request] = []

    async def embed(request: httpx.Request) -> httpx.Response:
        upstream_requests.append(request)
        return httpx.Response(
            200,
            json={"model": _MODEL, "data": [{"embedding": [1.0, 0.0], "index": 0}]},
        )

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(embed))
    app = create_app(_config(tmp_path), now=FakeClock(), http_client=upstream)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://coord") as client,
    ):
        state = app.state.coordinator
        await state.rag.create_collection("policies", _MODEL, 2)
        await state.rag.upsert(
            "policies",
            (
                Chunk(
                    chunk_id="planted",
                    text="The travel policy allows rail journeys.",
                    metadata={"source": "travel.md", "page": 3},
                    embedding=(1.0, 0.0),
                ),
                Chunk(
                    chunk_id="decoy",
                    text="The office has a shared kitchen.",
                    metadata={"source": "office.md"},
                    embedding=(0.0, 1.0),
                ),
            ),
        )
        allowed_key = await state.registry.create_api_key("rag-reader", [_MODEL])
        blocked_key = await state.registry.create_api_key("chat-reader", ["chat-model"])

        unavailable = await client.post(
            "/v1/rag/collections/policies/query",
            headers=bearer(allowed_key),
            json={"q": "How may I travel?", "k": 2},
        )
        forbidden = await client.post(
            "/v1/rag/collections/policies/query",
            headers=bearer(blocked_key),
            json={"q": "How may I travel?", "k": 2},
        )
        await enrolled_idle_agent(client, replicas=(make_replica(_MODEL),))
        found = await client.post(
            "/v1/rag/collections/policies/query",
            headers=bearer(allowed_key),
            json={"q": "How may I travel?", "k": 2},
        )

    assert unavailable.status_code == 503
    assert unavailable.json()["detail"] == (
        "no healthy embedding replica available for model 'bge-small'"
    )
    assert forbidden.status_code == 403
    assert len(upstream_requests) == 1
    assert upstream_requests[0].url == "http://127.0.0.1:8080/v1/embeddings"
    assert json.loads(upstream_requests[0].content) == {
        "model": _MODEL,
        "input": ["How may I travel?"],
    }
    assert found.status_code == 200, found.text
    body = found.json()
    assert body["collection"] == "policies"
    assert body["model_id"] == _MODEL
    assert [chunk["chunk_id"] for chunk in body["chunks"]] == ["planted", "decoy"]
    assert body["chunks"][0] == {
        "chunk_id": "planted",
        "text": "The travel policy allows rail journeys.",
        "score": 0.0,
        "metadata": {"source": "travel.md", "page": 3},
    }


@pytest.mark.asyncio
async def test_query_rejects_a_mismatched_embedding_model(tmp_path: Path) -> None:
    async def embed(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"model": "other-model", "data": [{"embedding": [1.0, 0.0], "index": 0}]},
        )

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(embed))
    app = create_app(_config(tmp_path), now=FakeClock(), http_client=upstream)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://coord") as client,
    ):
        state = app.state.coordinator
        await state.rag.create_collection("policies", _MODEL, 2)
        key = await state.registry.create_api_key("rag-reader", [_MODEL])
        await enrolled_idle_agent(client, replicas=(make_replica(_MODEL),))

        response = await client.post(
            "/v1/rag/collections/policies/query",
            headers=bearer(key),
            json={"q": "How may I travel?", "k": 1},
        )

    assert response.status_code == 502
    assert response.json()["detail"] == "embedding replica did not return model 'bge-small'"


@pytest.mark.asyncio
async def test_query_rejects_more_than_twenty_results(tmp_path: Path) -> None:
    app = create_app(_config(tmp_path), now=FakeClock())
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://coord") as client,
    ):
        key = await app.state.coordinator.registry.create_api_key("rag-reader", [_MODEL])
        response = await client.post(
            "/v1/rag/collections/policies/query",
            headers=bearer(key),
            json={"q": "How may I travel?", "k": 21},
        )

    assert response.status_code == 422
