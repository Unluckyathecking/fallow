# RAG vector store

`RagVectorStore` stores RAG collections and chunks in a sqlite-vec database. The
caller supplies the sibling database path, normally `rag.db` beside
`coordinator.db`.

## API

- `open()` loads sqlite-vec, enables WAL and foreign keys, then checks or creates
  schema version 1.
- `create_collection(name, model_id, dims)` creates a collection with fixed
  embedding dimensions. Repeating the same definition is idempotent. Changing
  the model or dimensions raises `CollectionConflictError`.
- `upsert(name, chunks)` inserts or replaces chunk text, canonical JSON metadata,
  and float32 embeddings in one transaction.
- `query(name, embedding, k)` returns the nearest chunks by L2 distance, with
  chunk ID as the stable secondary order.
- `list_collections()` returns collection definitions ordered by name.

The coordinator owns this store in production. It opens `rag.db` beside
`coordinator.db`, uses the same instance for ingestion and query routes, and
closes it during shutdown. See [`docs/rag.md`](../../../../../docs/rag.md) for
the public query contract and Open WebUI setup.

Call `close()` during coordinator shutdown. Operations on a closed store raise
`StoreNotOpenError`.

## Storage

`rag_collections` records the embedding model and dimension count.
`rag_chunks` stores stable chunk IDs, text, and metadata. Each collection owns a
`rag_vec_<collection_id>` virtual table whose `float[N]` declaration enforces the
collection dimension in sqlite-vec.

The database uses `PRAGMA user_version`. Version 0 migrates to version 1 in one
transaction. A newer version, an unversioned partial schema, or a missing vector
table stops the store from opening. The store does not repair or downgrade a
database automatically.

## Runtime constraint

The coordinator pins `sqlite-vec==0.1.9`. sqlite-vec is still pre-1.0 and its
Python binding is outside its stable SQL API. The store requires a Python whose
`sqlite3` was built with loadable-extension support. This is the default for
uv/python-build-standalone on Linux and Homebrew Python. The stock macOS system
Python and some CI images disable it.

The store checks this capability before creating `rag.db` and raises
`VectorExtensionError` with a direct explanation when it is unavailable.
