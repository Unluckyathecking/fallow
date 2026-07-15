from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class IngestChunk:
    chunk_id: str
    text: str
    metadata: Mapping[str, object]
    embedding: Sequence[float]


class VectorSink(Protocol):
    async def create_collection(self, name: str, model_id: str, dims: int) -> object: ...

    async def upsert(self, collection_name: str, chunks: Sequence[IngestChunk]) -> None: ...
