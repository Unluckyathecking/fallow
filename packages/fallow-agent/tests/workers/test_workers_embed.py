"""Unit tests for EmbedWorker against an httpx.MockTransport replica."""

import json

import httpx
import pytest
from workers_helpers import make_lease

from fallow_agent.workers import EmbedWorker, LocalEndpoint
from fallow_agent.workers.errors import WorkerBackendError, WorkerInputError


def _client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler)


def _endpoint(_model_id: str) -> LocalEndpoint:
    return LocalEndpoint(host="127.0.0.1", port=8081)


async def test_embed_request_shape_and_payload() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["host"] = request.url.host
        seen["port"] = request.url.port
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"embedding": [0.3, 0.4], "index": 1},
                    {"embedding": [0.1, 0.2], "index": 0},
                ],
                "model": "bge-small",
                "usage": {"total_tokens": 7},
            },
        )

    transport = httpx.MockTransport(handler)
    async with _client(transport) as client:
        worker = EmbedWorker(client=client, resolve_endpoint=_endpoint)
        output = await worker.run(make_lease(), json.dumps(["alpha", "beta"]).encode())

    assert seen["path"] == "/v1/embeddings"
    assert seen["host"] == "127.0.0.1"
    assert seen["port"] == 8081
    assert seen["body"] == {"model": "bge-small", "input": ["alpha", "beta"]}

    payload = json.loads(output.payload)
    # Response arrived out of order; embeddings must be sorted by index.
    assert payload == {
        "embeddings": [[0.1, 0.2], [0.3, 0.4]],
        "model_id": "bge-small",
        "dims": 2,
    }
    assert output.metrics.items == 2
    assert output.metrics.tokens == 7
    assert output.metrics.duration_s == 0.0


async def test_embed_rejects_non_array_input() -> None:
    async with _client(httpx.MockTransport(lambda r: httpx.Response(200))) as client:
        worker = EmbedWorker(client=client, resolve_endpoint=_endpoint)
        with pytest.raises(WorkerInputError):
            await worker.run(make_lease(), json.dumps({"not": "a list"}).encode())


async def test_embed_rejects_non_string_items() -> None:
    async with _client(httpx.MockTransport(lambda r: httpx.Response(200))) as client:
        worker = EmbedWorker(client=client, resolve_endpoint=_endpoint)
        with pytest.raises(WorkerInputError):
            await worker.run(make_lease(), json.dumps(["ok", 5]).encode())


async def test_embed_non_200_is_backend_error() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(503, text="unavailable"))
    async with _client(transport) as client:
        worker = EmbedWorker(client=client, resolve_endpoint=_endpoint)
        with pytest.raises(WorkerBackendError):
            await worker.run(make_lease(), json.dumps(["x"]).encode())


async def test_embed_count_mismatch_is_backend_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"embedding": [0.1], "index": 0}]})

    transport = httpx.MockTransport(handler)
    async with _client(transport) as client:
        worker = EmbedWorker(client=client, resolve_endpoint=_endpoint)
        with pytest.raises(WorkerBackendError):
            await worker.run(make_lease(), json.dumps(["a", "b"]).encode())
