"""Opt-in RAG retrieval into chat generation, end to end through the app.

A fake embed replica serves the query vector and a fake chat replica records the
prompt it is handed. With ``rag`` set, the planted chunk lands in that prompt as a
prepended context message; without it, the body is forwarded unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from app_helpers import ADMIN_KEY, FakeClock, bearer, enrolled_idle_agent, make_manifest
from httpx import ASGITransport

from fallow_coordinator.app import CoordinatorConfig, create_app
from fallow_coordinator.app.state import CoordinatorState
from fallow_coordinator.rag import Chunk
from fallow_protocol.capabilities import WorkerKind
from fallow_protocol.models import ReplicaState, ReplicaStatus

_CHAT_MODEL = "qwen2.5-7b"
_EMBED_MODEL = "bge-small"
_PLANTED = "The travel policy allows rail journeys."


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
        admission_timeout_s=0,
    )


def _upstream(chat_bodies: list[bytes]) -> httpx.AsyncClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/embeddings":
            return httpx.Response(
                200, json={"model": _EMBED_MODEL, "data": [{"embedding": [1.0, 0.0], "index": 0}]}
            )
        if request.url.path == "/v1/chat/completions":
            chat_bodies.append(request.content)
            return httpx.Response(
                200,
                json={
                    "id": "cmpl-1",
                    "object": "chat.completion",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
                },
            )
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _seed(state: CoordinatorState) -> str:
    """Register the chat model, plant a corpus, and return a key for both models."""
    await state.registry.put_model(make_manifest(_CHAT_MODEL, WorkerKind.CHAT), "blob://chat")
    await state.rag.create_collection("policies", _EMBED_MODEL, 2)
    await state.rag.upsert(
        "policies",
        (
            Chunk(
                chunk_id="planted",
                text=_PLANTED,
                metadata={"source": "travel.md"},
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
    return await state.registry.create_api_key("chat-user", [_CHAT_MODEL, _EMBED_MODEL])


def _replicas() -> tuple[ReplicaStatus, ...]:
    return (
        ReplicaStatus(model_id=_EMBED_MODEL, port=8080, state=ReplicaState.READY, inflight=0),
        ReplicaStatus(model_id=_CHAT_MODEL, port=8081, state=ReplicaState.READY, inflight=0),
    )


def _chat_body(with_rag: bool) -> dict[str, object]:
    body: dict[str, object] = {
        "model": _CHAT_MODEL,
        "messages": [{"role": "user", "content": "How may I travel?"}],
    }
    if with_rag:
        body["rag"] = {"collection": "policies", "k": 2}
    return body


@pytest.mark.asyncio
async def test_rag_chat_prepends_the_planted_chunk_and_logs_the_count(tmp_path: Path) -> None:
    chat_bodies: list[bytes] = []
    config = _config(tmp_path)
    app = create_app(config, now=FakeClock(), http_client=_upstream(chat_bodies))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://coord") as client,
    ):
        state = app.state.coordinator
        key = await _seed(state)
        await enrolled_idle_agent(client, replicas=_replicas())

        grounded = await client.post(
            "/v1/chat/completions", headers=bearer(key), json=_chat_body(with_rag=True)
        )
        plain = await client.post(
            "/v1/chat/completions", headers=bearer(key), json=_chat_body(with_rag=False)
        )

    assert grounded.status_code == 200, grounded.text
    assert plain.status_code == 200, plain.text

    forwarded = json.loads(chat_bodies[0])
    assert "rag" not in forwarded
    assert forwarded["messages"][0]["role"] == "system"
    assert _PLANTED in forwarded["messages"][0]["content"]
    assert forwarded["messages"][1] == {"role": "user", "content": "How may I travel?"}

    assert json.loads(chat_bodies[1]) == _chat_body(with_rag=False)

    logged = [json.loads(line) for line in config.gateway_log_path.read_text().splitlines()]
    assert logged[0]["rag_k"] == 2
    assert logged[1]["rag_k"] is None


@pytest.mark.asyncio
async def test_rag_chat_rejects_an_oversized_k(tmp_path: Path) -> None:
    config = _config(tmp_path)
    app = create_app(config, now=FakeClock(), http_client=_upstream([]))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://coord") as client,
    ):
        state = app.state.coordinator
        key = await _seed(state)
        await enrolled_idle_agent(client, replicas=_replicas())

        body = _chat_body(with_rag=True)
        body["rag"] = {"collection": "policies", "k": 65}
        response = await client.post("/v1/chat/completions", headers=bearer(key), json=body)

    assert response.status_code == 422
    assert response.json()["error"]["type"] == "invalid_request_error"


@pytest.mark.asyncio
async def test_rag_chat_reports_an_unknown_collection(tmp_path: Path) -> None:
    config = _config(tmp_path)
    app = create_app(config, now=FakeClock(), http_client=_upstream([]))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://coord") as client,
    ):
        state = app.state.coordinator
        key = await _seed(state)
        await enrolled_idle_agent(client, replicas=_replicas())

        body = _chat_body(with_rag=True)
        body["rag"] = {"collection": "missing", "k": 2}
        response = await client.post("/v1/chat/completions", headers=bearer(key), json=body)

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_rag_chat_forbids_a_collection_outside_the_key_allowlist(tmp_path: Path) -> None:
    chat_bodies: list[bytes] = []
    config = _config(tmp_path)
    app = create_app(config, now=FakeClock(), http_client=_upstream(chat_bodies))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://coord") as client,
    ):
        state = app.state.coordinator
        await _seed(state)
        # A key cleared for the chat model but not the collection's embedding model.
        chat_only = await state.registry.create_api_key("chat-only", [_CHAT_MODEL])
        await enrolled_idle_agent(client, replicas=_replicas())

        response = await client.post(
            "/v1/chat/completions", headers=bearer(chat_only), json=_chat_body(with_rag=True)
        )

    assert response.status_code == 403
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert chat_bodies == []  # nothing was proxied to the chat replica


@pytest.mark.asyncio
async def test_rag_chat_reports_no_embedding_replica(tmp_path: Path) -> None:
    config = _config(tmp_path)
    app = create_app(config, now=FakeClock(), http_client=_upstream([]))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://coord") as client,
    ):
        state = app.state.coordinator
        key = await _seed(state)
        # Only the chat replica is live; the embedding model has none.
        await enrolled_idle_agent(
            client,
            replicas=(
                ReplicaStatus(
                    model_id=_CHAT_MODEL, port=8081, state=ReplicaState.READY, inflight=0
                ),
            ),
        )

        response = await client.post(
            "/v1/chat/completions", headers=bearer(key), json=_chat_body(with_rag=True)
        )

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "no_replica_available"
