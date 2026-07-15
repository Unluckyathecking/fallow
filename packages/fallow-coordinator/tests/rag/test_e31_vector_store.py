from __future__ import annotations

import sqlite3
from pathlib import Path

import aiosqlite
import pytest
import sqlite_vec  # type: ignore[import-untyped]

from fallow_coordinator.rag import (
    Chunk,
    CollectionConflictError,
    DimensionMismatchError,
    RagVectorStore,
    SchemaVersionError,
    StoreNotOpenError,
    VectorExtensionError,
)


def _chunk(
    chunk_id: str,
    embedding: tuple[float, ...],
    *,
    text: str | None = None,
    source: str = "handbook",
) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        text=text or f"text for {chunk_id}",
        metadata={"source": source, "page": 1},
        embedding=embedding,
    )


@pytest.mark.asyncio
async def test_collection_creation_is_idempotent_and_dimension_locked(tmp_path: Path) -> None:
    store = RagVectorStore(tmp_path / "rag.db")
    await store.open()
    try:
        created = await store.create_collection("policies", "bge-small", 3)
        repeated = await store.create_collection("policies", "bge-small", 3)
        await store.create_collection("curriculum", "bge-small", 3)

        assert repeated == created
        assert [item.name for item in await store.list_collections()] == [
            "curriculum",
            "policies",
        ]
        with pytest.raises(CollectionConflictError, match="already uses model"):
            await store.create_collection("policies", "other-model", 3)
        with pytest.raises(CollectionConflictError, match="3 dimensions"):
            await store.create_collection("policies", "bge-small", 4)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_upsert_and_nearest_neighbor_query_are_deterministic(tmp_path: Path) -> None:
    store = RagVectorStore(tmp_path / "rag.db")
    await store.open()
    try:
        await store.create_collection("policies", "bge-small", 2)
        await store.upsert(
            "policies",
            (
                _chunk("b", (1.0, 0.0)),
                _chunk("a", (0.0, 0.0)),
                _chunk("c", (4.0, 0.0)),
            ),
        )

        first = await store.query("policies", (0.1, 0.0), 2)
        second = await store.query("policies", (0.1, 0.0), 2)

        assert first == second
        assert [item.chunk_id for item in first] == ["a", "b"]
        assert first[0].metadata == {"page": 1, "source": "handbook"}

        await store.upsert("policies", (_chunk("a", (2.0, 0.0), text="revised", source="memo"),))
        updated = await store.query("policies", (0.1, 0.0), 3)
        assert [item.chunk_id for item in updated] == ["b", "a", "c"]
        assert updated[1].text == "revised"
        assert updated[1].metadata["source"] == "memo"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_upsert_rejects_mismatched_vectors_before_writing(tmp_path: Path) -> None:
    store = RagVectorStore(tmp_path / "rag.db")
    await store.open()
    try:
        await store.create_collection("policies", "bge-small", 2)
        await store.upsert("policies", (_chunk("existing", (0.0, 0.0)),))

        with pytest.raises(DimensionMismatchError, match="expected 2 dimensions"):
            await store.upsert(
                "policies",
                (_chunk("valid", (1.0, 1.0)), _chunk("invalid", (1.0, 2.0, 3.0))),
            )

        results = await store.query("policies", (0.0, 0.0), 10)
        assert [item.chunk_id for item in results] == ["existing"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_newer_schema_version_fails_without_mutating_database(tmp_path: Path) -> None:
    path = tmp_path / "rag.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 2")

    store = RagVectorStore(path)
    with pytest.raises(SchemaVersionError, match="newer than supported"):
        await store.open()
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (2,)
    with pytest.raises(StoreNotOpenError):
        await store.list_collections()


@pytest.mark.asyncio
async def test_extension_load_failure_is_explicit_and_leaves_store_closed(tmp_path: Path) -> None:
    async def fail(_db: aiosqlite.Connection) -> None:
        raise RuntimeError("extension disabled")

    store = RagVectorStore(tmp_path / "rag.db", load_extension=fail)

    with pytest.raises(VectorExtensionError, match="could not load"):
        await store.open()
    with pytest.raises(StoreNotOpenError):
        await store.list_collections()


@pytest.mark.asyncio
async def test_missing_collection_vector_table_fails_reopen(tmp_path: Path) -> None:
    path = tmp_path / "rag.db"
    store = RagVectorStore(path)
    await store.open()
    await store.create_collection("policies", "bge-small", 2)
    await store.close()
    with sqlite3.connect(path) as db:
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
        db.execute("DROP TABLE rag_vec_1")

    reopened = RagVectorStore(path)
    with pytest.raises(SchemaVersionError, match="missing vector table"):
        await reopened.open()
