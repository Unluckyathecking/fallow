"""Authenticated RAG query route over a live embedding replica."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import Field, field_validator

from fallow_coordinator.rag.models import SearchResult
from fallow_coordinator.rag.retrieval import (
    RetrievalError,
    RetrievalStore,
    _embedding_from_response,
    _embedding_url,
    find_collection,
    search_collection,
)
from fallow_coordinator.registry import ApiKeyInfo
from fallow_protocol.base import FallowModel
from fallow_protocol.messages import ReplicaEndpoint

# The store surface is shared verbatim with the retrieval core.
QueryStore = RetrievalStore

# Kept importable here for the embedding-response unit tests that predate the
# retrieval split.
__all__ = [
    "QueryChunk",
    "QueryRegistry",
    "QueryRequest",
    "QueryResponse",
    "QueryStore",
    "_embedding_from_response",
    "_embedding_url",
    "create_query_router",
]

_MAX_QUERY_RESULTS = 20


class QueryRegistry(Protocol):
    async def authenticate_api_key(self, bearer: str) -> ApiKeyInfo | None: ...

    async def replica_endpoints(
        self, model_id: str, now: datetime
    ) -> tuple[ReplicaEndpoint, ...]: ...


class QueryRequest(FallowModel):
    """Public query body."""

    q: str
    k: int = Field(gt=0, le=_MAX_QUERY_RESULTS)

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
        try:
            collection = await find_collection(store, collection_name)
            if not _allows(key, collection.model_id):
                raise HTTPException(
                    status_code=403,
                    detail=f"api key not permitted to use model '{collection.model_id}'",
                )
            matches = await search_collection(
                registry, store, client, now, collection, body.q, body.k
            )
        except RetrievalError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
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
