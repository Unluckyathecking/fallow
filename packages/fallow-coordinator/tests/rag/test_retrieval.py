"""The shared embed path: pick a replica, retry once before any byte."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import httpx
import pytest

from fallow_coordinator.rag.retrieval import RetrievalError, embed_query
from fallow_protocol.messages import ReplicaEndpoint

_MODEL = "bge-small"


def _now() -> datetime:
    return datetime(2026, 7, 16, tzinfo=UTC)


def _endpoint(host: str, port: int) -> ReplicaEndpoint:
    return ReplicaEndpoint(agent_id=f"agent-{host}", host=host, port=port, model_id=_MODEL)


class _Registry:
    def __init__(self, endpoints: tuple[ReplicaEndpoint, ...]) -> None:
        self._endpoints = endpoints

    async def replica_endpoints(self, model_id: str, now: datetime) -> tuple[ReplicaEndpoint, ...]:
        return self._endpoints


def _first(_model: str, replicas: Sequence[ReplicaEndpoint]) -> ReplicaEndpoint | None:
    return replicas[0] if replicas else None


def _embedding_payload(vector: tuple[float, ...]) -> dict[str, object]:
    return {"model": _MODEL, "data": [{"embedding": list(vector), "index": 0}]}


@pytest.mark.asyncio
async def test_embed_retries_past_a_dead_first_replica() -> None:
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.host == "dead":
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json=_embedding_payload((1.0, 0.0)))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    registry = _Registry((_endpoint("dead", 8080), _endpoint("healthy", 8081)))

    embedding = await embed_query(registry, client, _now, _MODEL, "how do I travel?", _first)
    await client.aclose()

    assert embedding == (1.0, 0.0)
    assert seen == ["dead", "healthy"]  # the stale pick was tried, then the healthy one


@pytest.mark.asyncio
async def test_embed_retries_past_a_first_replica_5xx() -> None:
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.host == "flaky":
            return httpx.Response(503)
        return httpx.Response(200, json=_embedding_payload((0.0, 1.0)))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    registry = _Registry((_endpoint("flaky", 8080), _endpoint("healthy", 8081)))

    embedding = await embed_query(registry, client, _now, _MODEL, "q", _first)
    await client.aclose()

    assert embedding == (0.0, 1.0)
    assert seen == ["flaky", "healthy"]


@pytest.mark.asyncio
async def test_embed_gives_up_after_the_retry_budget() -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("connection refused", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    registry = _Registry((_endpoint("a", 8080), _endpoint("b", 8081), _endpoint("c", 8082)))

    with pytest.raises(RetrievalError) as error:
        await embed_query(registry, client, _now, _MODEL, "q", _first)
    await client.aclose()

    assert error.value.status_code == 502
    assert attempts == 2  # the first pick plus exactly one retry, never the third replica


@pytest.mark.asyncio
async def test_embed_503_when_no_replica_is_registered() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with pytest.raises(RetrievalError) as error:
        await embed_query(_Registry(()), client, _now, _MODEL, "q", _first)
    await client.aclose()

    assert error.value.status_code == 503
