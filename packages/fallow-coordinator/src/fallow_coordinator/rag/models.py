from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Collection:
    collection_id: int
    name: str
    model_id: str
    dims: int


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    text: str
    metadata: Mapping[str, object]
    embedding: Sequence[float]


@dataclass(frozen=True)
class SearchResult:
    chunk_id: str
    text: str
    metadata: dict[str, object]
    distance: float
