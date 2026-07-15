"""Authenticated RAG query route over a live embedding replica."""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import Field, field_validator

from fallow_coordinator.rag.models import Collection, SearchResult
from fallow_coordinator.rag.store import DimensionMismatchError
from fallow_coordinator.registry import ApiKeyInfo
from fallow_protocol.base import FallowModel
from fallow_protocol.messages import ReplicaEndpoint

_EMBEDDINGS_PATH = "/v1/embeddings"
_EMBED_TIMEOUT_S = 30.0


class QueryRequest(FallowModel):
    """Public query body."""

    q: str
    k: int = Field(gt=0)

    @field_validator("q")
    @classmethod
    def _nonempty_query(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("q must not be empty")
        return stripped


class QueryChunk(FallowModel):
    """One nearest chunk. Lower scores are closer L2 matches."""

    chunk_id: str
    text: str
    score: float
    metadata: dict[str, object]


class QueryResponse(FallowModel):
    """Nearest chunks for one collection query."""

    collection: str
    model_id: str
    chunks: tuple[QueryChunk, ...]


class QueryRegistry(Protocol):
    async def authenticate_api_key(self, bearer: str) -> ApiKeyInfo | None: ...

    async def replica_endpoints(
        self, model_id: str, now: datetime
    ) -> tuple[ReplicaEndpoint, ...]: ...


class QueryStore(Protocol):
    async def list_collections(self) -> tuple[Collection, ...]: ...

    async def query(
        self, collection_name: str, embedding: tuple[float, ...], k: int
    ) -> tuple[SearchResult, ...]: ...


def create_query_router(
    registry: QueryRegistry,
    store: QueryStore,
    client: httpx.AsyncClient,
    now: Callable[[], datetime],
) -> APIRouter:
    """Build the API-key-authenticated collection query route."""
    router = APIRouter(prefix="/v1/rag")

    @router.post("/collections/{collection_name}/query", response_model=QueryResponse)
    async def query_collection(
        collection_name: str, body: QueryRequest, request: Request
    ) -> QueryResponse:
        key = await _authenticate(registry, request.headers.get("authorization"))
        collection = await _find_collection(store, collection_name)
        if not _allows(key, collection.model_id):
            raise HTTPException(
                status_code=403,
                detail=f"api key not permitted to use model '{collection.model_id}'",
            )
        embedding = await _embed_query(registry, client, now, collection.model_id, body.q)
        try:
            matches = await store.query(collection.name, embedding, body.k)
        except (DimensionMismatchError, ValueError) as exc:
            raise HTTPException(
                status_code=502, detail=f"query embedding is invalid: {exc}"
            ) from exc
        return QueryResponse(
            collection=collection.name,
            model_id=collection.model_id,
            chunks=tuple(_chunk(match) for match in matches),
        )

    return router


async def _authenticate(registry: QueryRegistry, authorization: str | None) -> ApiKeyInfo:
    token = _extract_bearer(authorization)
    info = None if token is None else await registry.authenticate_api_key(token)
    if info is None:
        raise HTTPException(status_code=401, detail="missing or invalid api key")
    return info


async def _find_collection(store: QueryStore, name: str) -> Collection:
    collection = next((item for item in await store.list_collections() if item.name == name), None)
    if collection is None:
        raise HTTPException(status_code=404, detail=f"collection '{name}' does not exist")
    return collection


async def _embed_query(
    registry: QueryRegistry,
    client: httpx.AsyncClient,
    now: Callable[[], datetime],
    model_id: str,
    query: str,
) -> tuple[float, ...]:
    endpoints = await registry.replica_endpoints(model_id, now())
    if not endpoints:
        raise HTTPException(
            status_code=503,
            detail=f"no healthy embedding replica available for model '{model_id}'",
        )
    endpoint = endpoints[0]
    try:
        response = await client.post(
            f"http://{endpoint.host}:{endpoint.port}{_EMBEDDINGS_PATH}",
            json={"model": model_id, "input": [query]},
            timeout=_EMBED_TIMEOUT_S,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="embedding replica request failed") from exc
    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"embedding replica returned HTTP {response.status_code}",
        )
    return _embedding_from_response(response)


def _embedding_from_response(response: httpx.Response) -> tuple[float, ...]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=502, detail="embedding replica returned invalid JSON"
        ) from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise HTTPException(
            status_code=502, detail="embedding replica returned an invalid data array"
        )
    raw = data[0].get("embedding")
    if not isinstance(raw, list) or not raw:
        raise HTTPException(status_code=502, detail="embedding replica returned no query vector")
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in raw):
        raise HTTPException(
            status_code=502, detail="embedding replica returned a nonnumeric vector"
        )
    embedding = tuple(float(value) for value in raw)
    if not all(math.isfinite(value) for value in embedding):
        raise HTTPException(status_code=502, detail="embedding replica returned a nonfinite vector")
    return embedding


def _chunk(result: SearchResult) -> QueryChunk:
    return QueryChunk(
        chunk_id=result.chunk_id,
        text=result.text,
        score=result.distance,
        metadata=result.metadata,
    )


def _allows(key: ApiKeyInfo, model_id: str) -> bool:
    return key.model_allowlist is None or model_id in key.model_allowlist


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()
