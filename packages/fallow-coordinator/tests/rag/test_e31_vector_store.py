from __future__ import annotations

import sqlite3
from pathlib import Path

import aiosqlite
import pytest

from fallow_coordinator.rag import (
    Chunk,
    CollectionConflictError,
    DimensionMismatchError,
    RagVectorStore,
    SchemaVersionError,
    StoreNotOpenError,
    VectorExtensionError,
    sqlite_extensions_available,
)
from fallow_coordinator.rag import store as store_module

requires_sqlite_extensions = pytest.mark.skipif(
    not sqlite_extensions_available(),
    reason="host Python sqlite3 does not support loadable extensions",
)


async def _skip_extension_load(_db: aiosqlite.Connection) -> None:
    return None


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
@requires_sqlite_extensions
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
@requires_sqlite_extensions
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
@requires_sqlite_extensions
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

    store = RagVectorStore(path, load_extension=_skip_extension_load)
    with pytest.raises(SchemaVersionError, match="newer than supported"):
        await store.open()
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (2,)
        assert db.execute("PRAGMA journal_mode").fetchone() == ("delete",)
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
async def test_incapable_sqlite_build_fails_before_creating_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_module, "sqlite_extensions_available", lambda: False)
    path = tmp_path / "rag.db"
    store = RagVectorStore(path)

    with pytest.raises(VectorExtensionError, match="lacks loadable-extension support"):
        await store.open()
    assert not path.exists()


@pytest.mark.asyncio
async def test_missing_collection_vector_table_fails_reopen(tmp_path: Path) -> None:
    path = tmp_path / "rag.db"
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE rag_collections (
                collection_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                model_id TEXT NOT NULL,
                dims INTEGER NOT NULL CHECK (dims > 0)
            );
            INSERT INTO rag_collections(collection_id, name, model_id, dims)
            VALUES (1, 'policies', 'bge-small', 2);
            PRAGMA user_version = 1;
            """
        )

    reopened = RagVectorStore(path, load_extension=_skip_extension_load)
    with pytest.raises(SchemaVersionError, match="missing vector table"):
        await reopened.open()


@pytest.mark.asyncio
async def test_mismatched_vector_table_dimensions_fail_reopen(tmp_path: Path) -> None:
    path = tmp_path / "rag.db"
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE rag_collections (
                collection_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                model_id TEXT NOT NULL,
                dims INTEGER NOT NULL CHECK (dims > 0)
            );
            INSERT INTO rag_collections(collection_id, name, model_id, dims)
            VALUES (1, 'policies', 'bge-small', 2);
            CREATE TABLE rag_vec_1 (embedding FLOAT[3]);
            PRAGMA user_version = 1;
            """
        )

    reopened = RagVectorStore(path, load_extension=_skip_extension_load)
    with pytest.raises(SchemaVersionError, match=r"does not match float\[2\]"):
        await reopened.open()
