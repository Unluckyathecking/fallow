from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

import httpx
import pytest
from app_helpers import ADMIN_KEY, FakeClock, admin_headers
from httpx import ASGITransport

from fallow_coordinator.app import CoordinatorConfig, create_app
from fallow_coordinator.app.rag_ingestion import (
    IngestChunk,
    IngestionPayloadError,
    IngestionService,
    IngestionState,
    _parse_payload,
    _result_path,
)
from fallow_coordinator.queue import SqliteQueueStore
from fallow_protocol.messages import WorkResult, WorkResultStatus

_MODEL = "bge-small"


class FakeVectorSink:
    def __init__(self) -> None:
        self.collections: dict[str, tuple[str, int]] = {}
        self.chunks: dict[str, IngestChunk] = {}
        self.upsert_calls = 0

    async def create_collection(self, name: str, model_id: str, dims: int) -> object:
        self.collections.setdefault(name, (model_id, dims))
        return self.collections[name]

    async def upsert(self, _collection_name: str, chunks: Sequence[IngestChunk]) -> None:
        self.upsert_calls += 1
        self.chunks.update((chunk.chunk_id, chunk) for chunk in chunks)


def _config(tmp_path: Path, *, chunks_per_unit: int = 1) -> CoordinatorConfig:
    return CoordinatorConfig(
        db_path=tmp_path / "coordinator.db",
        blob_dir=tmp_path / "blobs",
        unit_input_dir=tmp_path / "units",
        result_dir=tmp_path / "results",
        events_jsonl_path=tmp_path / "events.jsonl",
        gateway_log_path=tmp_path / "gateway.jsonl",
        admin_key=ADMIN_KEY,
        chunks_per_unit=chunks_per_unit,
        requeue_interval_s=3600,
    )


async def _service(
    tmp_path: Path, *, max_attempts: int = 3, chunks_per_unit: int = 1
) -> tuple[IngestionService, SqliteQueueStore, FakeVectorSink, CoordinatorConfig]:
    config = _config(tmp_path, chunks_per_unit=chunks_per_unit)
    config.unit_input_dir.mkdir(parents=True)
    config.result_dir.mkdir(parents=True)
    queue = SqliteQueueStore(config.db_path, max_attempts=max_attempts)
    await queue.init()
    sink = FakeVectorSink()
    service = IngestionService(
        queue=queue,
        sink=sink,
        corpus_dir=config.unit_input_dir / "rag-corpora",
        unit_input_dir=config.unit_input_dir,
        result_dir=config.result_dir,
        chunks_per_unit=chunks_per_unit,
    )
    return service, queue, sink, config


async def _complete_next(
    queue: SqliteQueueStore,
    config: CoordinatorConfig,
    *,
    agent_id: str = "agent-a",
    model_id: str = _MODEL,
) -> str:
    lease = await queue.lease_next(agent_id, [_MODEL])
    assert lease is not None
    texts = json.loads((config.unit_input_dir / lease.input_url).read_bytes())
    payload = json.dumps(
        {
            "embeddings": [[float(index), 1.0] for index, _ in enumerate(texts)],
            "model_id": model_id,
            "dims": 2,
        },
        separators=(",", ":"),
    ).encode()
    digest = hashlib.sha256(payload).hexdigest()
    (config.result_dir / digest).write_bytes(payload)
    assert await queue.bind_result_payload(
        agent_id, lease.work_unit_id, lease.attempt, digest, digest
    )
    assert await queue.complete_unit(
        agent_id,
        lease.attempt,
        WorkResult(
            work_unit_id=lease.work_unit_id,
            status=WorkResultStatus.SUCCEEDED,
            result_ref=digest,
        ),
    )
    return lease.work_unit_id


@pytest.mark.asyncio
async def test_ingestion_finalizes_completed_payloads_and_is_incremental(tmp_path: Path) -> None:
    service, queue, sink, config = await _service(tmp_path, chunks_per_unit=2)
    try:
        submitted = await service.submit("policies", _MODEL, ("alpha", "beta"))
        running = await service.status("policies", submitted.job_id)
        assert running.state is IngestionState.RUNNING

        await _complete_next(queue, config)
        ready = await service.status("policies", submitted.job_id)

        assert ready.state is IngestionState.READY
        assert ready.indexed_chunks == 2
        assert ready.dead_units == 0
        assert sink.collections == {"policies": (_MODEL, 2)}
        assert set(sink.chunks) == {
            hashlib.sha256(b"alpha").hexdigest(),
            hashlib.sha256(b"beta").hexdigest(),
        }

        duplicate = await service.submit("policies", _MODEL, ("alpha", "beta"))
        duplicate_status = await service.status("policies", duplicate.job_id)
        assert duplicate_status.state is IngestionState.READY
        assert duplicate_status.indexed_chunks == 2
        assert len(sink.chunks) == 2
    finally:
        await queue.close()


@pytest.mark.asyncio
async def test_partial_ingestion_indexes_done_units_and_reports_dead_count(tmp_path: Path) -> None:
    service, queue, sink, config = await _service(tmp_path, max_attempts=1)
    try:
        submitted = await service.submit("policies", _MODEL, ("will-die", "will-finish"))
        failed = await queue.lease_next("agent-a", [_MODEL])
        assert failed is not None
        assert await queue.requeue_agent("agent-a") == 1
        await _complete_next(queue, config, agent_id="agent-b")

        status = await service.status("policies", submitted.job_id)

        assert status.state is IngestionState.PARTIAL
        assert status.total_units == 2
        assert status.done_units == 1
        assert status.dead_units == 1
        assert status.indexed_chunks == 1
        assert len(sink.chunks) == 1
    finally:
        await queue.close()


@pytest.mark.asyncio
async def test_repeated_text_in_one_upload_indexes_one_content_chunk(tmp_path: Path) -> None:
    service, queue, sink, config = await _service(tmp_path, chunks_per_unit=2)
    try:
        submitted = await service.submit("policies", _MODEL, ("same", "same"))
        await _complete_next(queue, config)

        status = await service.status("policies", submitted.job_id)

        assert status.state is IngestionState.READY
        assert status.indexed_chunks == 1
        assert list(sink.chunks) == [hashlib.sha256(b"same").hexdigest()]
    finally:
        await queue.close()


@pytest.mark.asyncio
async def test_ingestion_rejects_result_from_a_different_model(tmp_path: Path) -> None:
    service, queue, _sink, config = await _service(tmp_path)
    try:
        submitted = await service.submit("policies", _MODEL, ("policy",))
        await _complete_next(queue, config, model_id="wrong-model")

        with pytest.raises(IngestionPayloadError, match="expected 'bge-small'"):
            await service.status("policies", submitted.job_id)
    finally:
        await queue.close()


@pytest.mark.parametrize(
    "payload",
    [
        {"model_id": _MODEL, "dims": 2, "embeddings": [[1.0]]},
        {"model_id": _MODEL, "dims": True, "embeddings": [[1.0]]},
        {"model_id": _MODEL, "dims": 1, "embeddings": [[True]]},
        {"model_id": _MODEL, "dims": 1, "embeddings": [[float("inf")]]},
    ],
)
def test_result_payload_rejects_bad_rows_and_dimensions(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    path = tmp_path / "payload"
    path.write_text(json.dumps(payload))

    with pytest.raises(IngestionPayloadError):
        _parse_payload(path, 1)


def test_result_reference_must_be_a_lowercase_sha256(tmp_path: Path) -> None:
    with pytest.raises(IngestionPayloadError, match="invalid payload reference"):
        _result_path(tmp_path, "../payload")


@pytest.mark.asyncio
async def test_document_upload_route_requires_admin_and_submits_embedding_job(
    tmp_path: Path,
) -> None:
    sink = FakeVectorSink()
    app = create_app(_config(tmp_path), now=FakeClock(), vector_sink=sink)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://coord") as client,
    ):
        body = {"model_id": _MODEL, "chunks": ["policy text"]}
        unauthorized = await client.post("/v1/admin/rag/collections/policies/documents", json=body)
        accepted = await client.post(
            "/v1/admin/rag/collections/policies/documents",
            json=body,
            headers=admin_headers(),
        )

    assert unauthorized.status_code == 401
    assert accepted.status_code == 202
    assert accepted.json()["state"] == "running"
    assert accepted.json()["total_units"] == 1
