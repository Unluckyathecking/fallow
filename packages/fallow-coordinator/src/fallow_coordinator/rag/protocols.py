from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from fallow_coordinator.rag.models import Chunk

IngestChunk = Chunk


class VectorSink(Protocol):
    async def create_collection(self, name: str, model_id: str, dims: int) -> object: ...

    async def upsert(self, collection_name: str, chunks: Sequence[IngestChunk]) -> None: ...
