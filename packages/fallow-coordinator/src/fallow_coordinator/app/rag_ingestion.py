from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from fallow_coordinator.app.chunker import ChunkError, chunk_job
from fallow_coordinator.queue import JobDetails, SqliteQueueStore
from fallow_coordinator.rag import IngestChunk, VectorSink
from fallow_protocol.capabilities import WorkerKind
from fallow_protocol.messages import (
    JobState,
    JobStatus,
    JobSubmit,
    WorkResultStatus,
    WorkUnitState,
)

_COLLECTION_PARAM = "rag_collection"


class IngestionState(StrEnum):
    RUNNING = "running"
    READY = "ready"
    PARTIAL = "partial"


@dataclass(frozen=True)
class IngestionStatus:
    ingestion_id: str
    state: IngestionState
    total_units: int
    done_units: int
    dead_units: int
    indexed_chunks: int


class IngestionError(RuntimeError):
    pass


class IngestionNotFoundError(IngestionError):
    pass


class IngestionPayloadError(IngestionError):
    pass


class IngestionService:
    def __init__(
        self,
        *,
        queue: SqliteQueueStore,
        sink: VectorSink,
        corpus_dir: Path,
        unit_input_dir: Path,
        result_dir: Path,
        chunks_per_unit: int,
    ) -> None:
        self._queue = queue
        self._sink = sink
        self._corpus_dir = corpus_dir
        self._unit_input_dir = unit_input_dir
        self._result_dir = result_dir
        self._chunks_per_unit = chunks_per_unit

    async def submit(self, collection: str, model_id: str, texts: Sequence[str]) -> JobStatus:
        _require_text("collection", collection)
        _require_text("model id", model_id)
        if not texts:
            raise ValueError("document upload must contain at least one chunk")
        for text in texts:
            _require_text("chunk text", text)
        corpus = self._store_corpus(texts)
        job = JobSubmit(
            kind=WorkerKind.EMBED,
            model_id=model_id,
            payload_ref=str(corpus),
            params={_COLLECTION_PARAM: collection},
        )
        try:
            units = chunk_job(job, self._unit_input_dir, self._chunks_per_unit)
        except ChunkError as exc:  # pragma: no cover - validated corpus is always readable
            raise IngestionError(str(exc)) from exc
        job_id = await self._queue.submit_job(job, units, reuse_active=True)
        status = await self._queue.job_status(job_id)
        if status is None:  # pragma: no cover - queue returns the submitted job
            raise IngestionError("ingestion job vanished after submission")
        return status

    async def status(self, collection: str, ingestion_id: str) -> IngestionStatus:
        status = await self._queue.job_status(ingestion_id)
        details = await self._queue.job_details(ingestion_id)
        if status is None or details is None:
            raise IngestionNotFoundError(f"unknown ingestion: {ingestion_id}")
        if details.params.get(_COLLECTION_PARAM) != collection:
            raise IngestionNotFoundError(f"unknown ingestion: {ingestion_id}")
        if status.state is not JobState.DONE:
            return _status(status, IngestionState.RUNNING, 0)
        indexed = await self._queue.job_finalization(ingestion_id)
        if indexed is None:
            indexed = await self._finalize(collection, details)
            indexed = await self._queue.mark_job_finalized(ingestion_id, indexed)
        failed = any(unit.result_status is WorkResultStatus.FAILED for unit in details.units)
        state = IngestionState.PARTIAL if status.dead_units or failed else IngestionState.READY
        return _status(status, state, indexed)

    def _store_corpus(self, texts: Sequence[str]) -> Path:
        lines = [
            json.dumps({"id": _chunk_id(text), "text": text}, separators=(",", ":"))
            for text in texts
        ]
        payload = ("\n".join(lines) + "\n").encode()
        digest = hashlib.sha256(payload).hexdigest()
        self._corpus_dir.mkdir(parents=True, exist_ok=True)
        target = self._corpus_dir / f"{digest}.jsonl"
        if not target.exists():
            temporary = self._corpus_dir / f".{digest}.{uuid4().hex}.tmp"
            try:
                temporary.write_bytes(payload)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        return target

    async def _finalize(self, collection: str, details: JobDetails) -> int:
        chunks: dict[str, IngestChunk] = {}
        dims: int | None = None
        for unit in details.units:
            if unit.state is WorkUnitState.DEAD:
                continue
            if unit.result_status is WorkResultStatus.FAILED:
                continue
            if unit.state is not WorkUnitState.DONE or unit.result_ref is None:
                raise IngestionPayloadError(
                    f"completed ingestion unit {unit.work_unit_id} has no payload"
                )
            texts = _parse_texts(self._unit_input_dir / unit.input_ref)
            model_id, payload_dims, embeddings = _parse_payload(
                _result_path(self._result_dir, unit.result_ref), len(texts)
            )
            if model_id != details.model_id:
                raise IngestionPayloadError(
                    f"unit {unit.work_unit_id} returned model {model_id!r}, "
                    f"expected {details.model_id!r}"
                )
            if dims is not None and dims != payload_dims:
                raise IngestionPayloadError("ingestion units returned inconsistent dimensions")
            dims = payload_dims
            for text, embedding in zip(texts, embeddings, strict=True):
                chunk = IngestChunk(
                    chunk_id=_chunk_id(text),
                    text=text,
                    metadata={},
                    embedding=embedding,
                )
                chunks[chunk.chunk_id] = chunk
        if chunks:
            assert dims is not None
            await self._sink.create_collection(collection, details.model_id, dims)
            await self._sink.upsert(collection, tuple(chunks.values()))
        return len(chunks)


def _status(status: JobStatus, state: IngestionState, indexed: int) -> IngestionStatus:
    return IngestionStatus(
        ingestion_id=status.job_id,
        state=state,
        total_units=status.total_units,
        done_units=status.done_units,
        dead_units=status.dead_units,
        indexed_chunks=indexed,
    )


def _parse_texts(path: Path) -> tuple[str, ...]:
    try:
        value: Any = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise IngestionPayloadError(f"could not read unit input {path.name}") from exc
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise IngestionPayloadError(f"unit input {path.name} is not a string array")
    return tuple(value)


def _parse_payload(path: Path, expected: int) -> tuple[str, int, tuple[tuple[float, ...], ...]]:
    try:
        value: Any = json.loads(path.read_bytes())
        model_id = value["model_id"]
        dims = value["dims"]
        raw_embeddings = value["embeddings"]
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError) as exc:
        raise IngestionPayloadError(f"could not parse result payload {path.name}") from exc
    if (
        not isinstance(model_id, str)
        or not isinstance(dims, int)
        or isinstance(dims, bool)
        or dims <= 0
    ):
        raise IngestionPayloadError(f"result payload {path.name} has invalid model metadata")
    if not isinstance(raw_embeddings, list) or len(raw_embeddings) != expected:
        actual = len(raw_embeddings) if isinstance(raw_embeddings, list) else "invalid"
        raise IngestionPayloadError(
            f"result payload {path.name} has {actual} rows, expected {expected}"
        )
    embeddings: list[tuple[float, ...]] = []
    for raw in raw_embeddings:
        if (
            not isinstance(raw, list)
            or len(raw) != dims
            or not all(
                isinstance(item, (int, float))
                and not isinstance(item, bool)
                and math.isfinite(float(item))
                for item in raw
            )
        ):
            raise IngestionPayloadError(f"result payload {path.name} has an invalid embedding")
        embeddings.append(tuple(float(item) for item in raw))
    return model_id, dims, tuple(embeddings)


def _chunk_id(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _result_path(result_dir: Path, result_ref: str) -> Path:
    if len(result_ref) != 64 or any(
        character not in "0123456789abcdef" for character in result_ref
    ):
        raise IngestionPayloadError("completed ingestion unit has an invalid payload reference")
    return result_dir / result_ref


def _require_text(label: str, value: str) -> None:
    if not value.strip():
        raise ValueError(f"{label} must not be empty")
