from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, cast

import aiosqlite
import sqlite_vec  # type: ignore[import-untyped]

from fallow_coordinator.rag.models import Chunk, Collection, SearchResult

_SCHEMA_VERSION = 1
_SCHEMA = """
CREATE TABLE rag_collections (
    collection_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    model_id TEXT NOT NULL,
    dims INTEGER NOT NULL CHECK (dims > 0)
);
CREATE TABLE rag_chunks (
    row_id INTEGER PRIMARY KEY,
    collection_id INTEGER NOT NULL REFERENCES rag_collections(collection_id) ON DELETE CASCADE,
    chunk_id TEXT NOT NULL,
    text TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    UNIQUE (collection_id, chunk_id)
);
CREATE INDEX rag_chunks_collection_idx ON rag_chunks(collection_id);
"""

ExtensionLoader = Callable[[aiosqlite.Connection], Awaitable[None]]


class RagStoreError(RuntimeError):
    pass


class StoreNotOpenError(RagStoreError):
    pass


class CollectionConflictError(RagStoreError):
    pass


class CollectionNotFoundError(RagStoreError):
    pass


class DimensionMismatchError(RagStoreError):
    pass


class SchemaVersionError(RagStoreError):
    pass


class VectorExtensionError(RagStoreError):
    pass


async def _load_sqlite_vec(db: aiosqlite.Connection) -> None:
    await db.enable_load_extension(True)
    try:
        await db.load_extension(sqlite_vec.loadable_path())
    finally:
        await db.enable_load_extension(False)


class RagVectorStore:
    """Async sqlite-vec store with fixed dimensions per named collection."""

    def __init__(self, path: Path, *, load_extension: ExtensionLoader = _load_sqlite_vec) -> None:
        self._path = path
        self._load_extension = load_extension
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        if self._db is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(self._path)
        db.row_factory = aiosqlite.Row
        try:
            await self._load_extension(db)
        except Exception as exc:
            await db.close()
            raise VectorExtensionError("could not load the pinned sqlite-vec extension") from exc
        try:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("PRAGMA journal_mode = WAL")
            await self._migrate(db)
        except Exception:
            await db.close()
            raise
        self._db = db

    async def close(self) -> None:
        if self._db is None:
            return
        await self._db.close()
        self._db = None

    async def create_collection(self, name: str, model_id: str, dims: int) -> Collection:
        _require_text("collection name", name)
        _require_text("model id", model_id)
        if dims <= 0:
            raise ValueError("collection dimensions must be positive")
        async with self._lock:
            db = self._require_db()
            existing = await self._collection_by_name(db, name)
            if existing is not None:
                if existing.model_id != model_id or existing.dims != dims:
                    raise CollectionConflictError(
                        f"collection {name!r} already uses model {existing.model_id!r} "
                        f"with {existing.dims} dimensions"
                    )
                return existing
            try:
                cursor = await db.execute(
                    "INSERT INTO rag_collections(name, model_id, dims) VALUES (?, ?, ?)",
                    (name, model_id, dims),
                )
                collection_id = cursor.lastrowid
                if collection_id is None:  # pragma: no cover - SQLite always supplies rowid
                    raise RagStoreError("collection insert did not return an id")
                await db.execute(_vector_table_sql(collection_id, dims))
                await db.commit()
            except Exception:
                await db.rollback()
                raise
            return Collection(collection_id, name, model_id, dims)

    async def list_collections(self) -> tuple[Collection, ...]:
        async with self._lock:
            cursor = await self._require_db().execute(
                "SELECT collection_id, name, model_id, dims FROM rag_collections ORDER BY name"
            )
            rows = await cursor.fetchall()
        return tuple(_collection(row) for row in rows)

    async def upsert(self, collection_name: str, chunks: Sequence[Chunk]) -> None:
        if not chunks:
            return
        async with self._lock:
            db = self._require_db()
            collection = await self._require_collection(db, collection_name)
            prepared = tuple(_prepare_chunk(chunk, collection.dims) for chunk in chunks)
            if len({chunk.chunk_id for chunk, _, _ in prepared}) != len(prepared):
                raise ValueError("one upsert batch cannot contain duplicate chunk ids")
            table = _vector_table_name(collection.collection_id)
            try:
                for chunk, metadata_json, embedding in prepared:
                    row_id = await self._upsert_chunk_row(
                        db, collection.collection_id, chunk, metadata_json
                    )
                    await db.execute(f"DELETE FROM {table} WHERE rowid = ?", (row_id,))
                    await db.execute(
                        f"INSERT INTO {table}(rowid, embedding) VALUES (?, ?)",
                        (row_id, embedding),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def query(
        self, collection_name: str, embedding: Sequence[float], k: int
    ) -> tuple[SearchResult, ...]:
        if k <= 0:
            raise ValueError("query result count must be positive")
        async with self._lock:
            db = self._require_db()
            collection = await self._require_collection(db, collection_name)
            vector = _serialize_embedding(embedding, collection.dims)
            table = _vector_table_name(collection.collection_id)
            cursor = await db.execute(
                f"""
                SELECT c.chunk_id, c.text, c.metadata_json, v.distance
                FROM {table} AS v
                JOIN rag_chunks AS c ON c.row_id = v.rowid
                WHERE v.embedding MATCH ? AND k = ?
                ORDER BY v.distance, c.chunk_id
                """,
                (vector, k),
            )
            rows = await cursor.fetchall()
        return tuple(
            SearchResult(
                chunk_id=str(row["chunk_id"]),
                text=str(row["text"]),
                metadata=_metadata(str(row["metadata_json"])),
                distance=float(row["distance"]),
            )
            for row in rows
        )

    async def _migrate(self, db: aiosqlite.Connection) -> None:
        row = await (await db.execute("PRAGMA user_version")).fetchone()
        if row is None:  # pragma: no cover - PRAGMA always returns one row
            raise SchemaVersionError("rag database did not report a schema version")
        version = int(row[0])
        if version > _SCHEMA_VERSION:
            raise SchemaVersionError(
                f"rag database schema {version} is newer than supported {_SCHEMA_VERSION}"
            )
        if version == _SCHEMA_VERSION:
            await self._validate_schema(db)
            return
        existing = await (
            await db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'rag_%' LIMIT 1")
        ).fetchone()
        if existing is not None:
            raise SchemaVersionError("rag database has unversioned rag_ tables")
        try:
            await db.executescript(f"BEGIN;\n{_SCHEMA}\nPRAGMA user_version = 1;\nCOMMIT;")
        except Exception:
            await db.rollback()
            raise

    async def _validate_schema(self, db: aiosqlite.Connection) -> None:
        try:
            rows = await (await db.execute("SELECT collection_id FROM rag_collections")).fetchall()
        except aiosqlite.Error as exc:
            raise SchemaVersionError("rag database schema is incomplete") from exc
        for row in rows:
            table = _vector_table_name(int(row["collection_id"]))
            found = await (
                await db.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (table,))
            ).fetchone()
            if found is None:
                raise SchemaVersionError(f"rag database is missing vector table {table}")

    def _require_db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise StoreNotOpenError("rag vector store is not open")
        return self._db

    async def _require_collection(self, db: aiosqlite.Connection, name: str) -> Collection:
        collection = await self._collection_by_name(db, name)
        if collection is None:
            raise CollectionNotFoundError(f"collection {name!r} does not exist")
        return collection

    async def _collection_by_name(self, db: aiosqlite.Connection, name: str) -> Collection | None:
        cursor = await db.execute(
            "SELECT collection_id, name, model_id, dims FROM rag_collections WHERE name = ?",
            (name,),
        )
        row = await cursor.fetchone()
        return None if row is None else _collection(row)

    async def _upsert_chunk_row(
        self,
        db: aiosqlite.Connection,
        collection_id: int,
        chunk: Chunk,
        metadata_json: str,
    ) -> int:
        cursor = await db.execute(
            "SELECT row_id FROM rag_chunks WHERE collection_id = ? AND chunk_id = ?",
            (collection_id, chunk.chunk_id),
        )
        row = await cursor.fetchone()
        if row is not None:
            row_id = int(row["row_id"])
            await db.execute(
                "UPDATE rag_chunks SET text = ?, metadata_json = ? WHERE row_id = ?",
                (chunk.text, metadata_json, row_id),
            )
            return row_id
        inserted = await db.execute(
            "INSERT INTO rag_chunks(collection_id, chunk_id, text, metadata_json) "
            "VALUES (?, ?, ?, ?)",
            (collection_id, chunk.chunk_id, chunk.text, metadata_json),
        )
        inserted_id = inserted.lastrowid
        if inserted_id is None:  # pragma: no cover - SQLite always supplies rowid
            raise RagStoreError("chunk insert did not return an id")
        return inserted_id


def _vector_table_name(collection_id: int) -> str:
    return f"rag_vec_{collection_id}"


def _vector_table_sql(collection_id: int, dims: int) -> str:
    table = _vector_table_name(collection_id)
    return f"CREATE VIRTUAL TABLE {table} USING vec0(embedding float[{dims}])"


def _collection(row: aiosqlite.Row) -> Collection:
    return Collection(
        collection_id=int(row["collection_id"]),
        name=str(row["name"]),
        model_id=str(row["model_id"]),
        dims=int(row["dims"]),
    )


def _prepare_chunk(chunk: Chunk, dims: int) -> tuple[Chunk, str, bytes]:
    _require_text("chunk id", chunk.chunk_id)
    try:
        metadata_json = json.dumps(
            dict(chunk.metadata), sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"chunk {chunk.chunk_id!r} metadata is not JSON serializable") from exc
    return chunk, metadata_json, _serialize_embedding(chunk.embedding, dims)


def _serialize_embedding(embedding: Sequence[float], dims: int) -> bytes:
    if len(embedding) != dims:
        raise DimensionMismatchError(f"expected {dims} dimensions, received {len(embedding)}")
    values = tuple(float(value) for value in embedding)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("embedding values must be finite")
    return cast(bytes, sqlite_vec.serialize_float32(values))


def _metadata(raw: str) -> dict[str, object]:
    value: Any = json.loads(raw)
    if not isinstance(value, dict):  # pragma: no cover - writes always store an object
        raise RagStoreError("stored chunk metadata is not an object")
    return {str(key): item for key, item in value.items()}


def _require_text(label: str, value: str) -> None:
    if not value.strip():
        raise ValueError(f"{label} must not be empty")
