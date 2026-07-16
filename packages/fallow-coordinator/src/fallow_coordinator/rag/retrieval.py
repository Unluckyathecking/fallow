"""Shared collection retrieval: embed a query, then vec-search a collection.

Both the public RAG query route and the gateway's opt-in retrieval-into-chat run
the same two steps — embed the text on a live replica, search the collection —
so that logic lives here once. Callers map :class:`RetrievalError` onto their own
wire shape (the query route to FastAPI's ``{"detail": ...}``, the gateway to the
OpenAI ``{"error": {...}}`` envelope).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

import httpx

from fallow_coordinator.rag.models import Collection, SearchResult
from fallow_coordinator.rag.store import DimensionMismatchError
from fallow_protocol.messages import ReplicaEndpoint

_EMBEDDINGS_PATH = "/v1/embeddings"
_EMBED_TIMEOUT_S = 30.0


class RetrievalError(Exception):
    """A retrieval failure carrying the HTTP status and a client-safe detail."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class RetrievalRegistry(Protocol):
    async def replica_endpoints(
        self, model_id: str, now: datetime
    ) -> tuple[ReplicaEndpoint, ...]: ...


class RetrievalStore(Protocol):
    async def list_collections(self) -> tuple[Collection, ...]: ...

    async def query(
        self, collection_name: str, embedding: tuple[float, ...], k: int
    ) -> tuple[SearchResult, ...]: ...


async def search_collection(
    registry: RetrievalRegistry,
    store: RetrievalStore,
    client: httpx.AsyncClient,
    now: Callable[[], datetime],
    collection: Collection,
    query: str,
    k: int,
) -> tuple[SearchResult, ...]:
    """Embed ``query`` for an already-resolved ``collection`` and vec-search it."""
    embedding = await embed_query(registry, client, now, collection.model_id, query)
    try:
        return await store.query(collection.name, embedding, k)
    except (DimensionMismatchError, ValueError) as exc:
        raise RetrievalError(502, f"query embedding is invalid: {exc}") from exc


async def find_collection(store: RetrievalStore, name: str) -> Collection:
    collection = next((item for item in await store.list_collections() if item.name == name), None)
    if collection is None:
        raise RetrievalError(404, f"collection '{name}' does not exist")
    return collection


async def embed_query(
    registry: RetrievalRegistry,
    client: httpx.AsyncClient,
    now: Callable[[], datetime],
    model_id: str,
    query: str,
) -> tuple[float, ...]:
    endpoints = await registry.replica_endpoints(model_id, now())
    if not endpoints:
        raise RetrievalError(503, f"no healthy embedding replica available for model '{model_id}'")
    endpoint = endpoints[0]
    try:
        response = await client.post(
            _embedding_url(endpoint),
            json={"model": model_id, "input": [query]},
            timeout=_EMBED_TIMEOUT_S,
        )
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise RetrievalError(502, "embedding replica request failed") from exc
    if response.status_code != 200:
        raise RetrievalError(502, f"embedding replica returned HTTP {response.status_code}")
    return _embedding_from_response(response, model_id)


def _embedding_from_response(response: httpx.Response, model_id: str) -> tuple[float, ...]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise RetrievalError(502, "embedding replica returned invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("model") != model_id:
        raise RetrievalError(502, f"embedding replica did not return model '{model_id}'")
    data = payload.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise RetrievalError(502, "embedding replica returned an invalid data array")
    raw = data[0].get("embedding")
    if not isinstance(raw, list) or not raw:
        raise RetrievalError(502, "embedding replica returned no query vector")
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in raw):
        raise RetrievalError(502, "embedding replica returned a nonnumeric vector")
    try:
        embedding = tuple(float(value) for value in raw)
    except OverflowError as exc:
        raise RetrievalError(502, "embedding replica returned an out-of-range vector") from exc
    if not all(math.isfinite(value) for value in embedding):
        raise RetrievalError(502, "embedding replica returned a nonfinite vector")
    return embedding


def _embedding_url(endpoint: ReplicaEndpoint) -> httpx.URL:
    return httpx.URL(
        scheme="http",
        host=endpoint.host,
        port=endpoint.port,
        path=_EMBEDDINGS_PATH,
    )
