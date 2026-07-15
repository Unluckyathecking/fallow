# ADR 032: RAG vector store

**Status:** accepted
**Date:** 2026-07-15

## Context

RAG ingestion needs local vector storage for chunk IDs, source text, metadata,
and embeddings. The coordinator already has a shared SQLite database for its
registry and work queue. sqlite-vec adds vector tables through a loadable SQLite
extension and produces a write pattern that differs from the queue's short
lease transactions.

Collections may use different embedding models. The store must reject vectors
whose dimensions do not match the model used to create a collection.

## Decision

RAG data lives in a sibling `rag.db`, not in `coordinator.db`. The RAG connection
alone enables extension loading and loads the pinned sqlite-vec binary. Registry
and queue connections never enable extension loading. The separate WAL also
keeps vector upserts from competing with lease transactions in the same file.

The schema uses only `rag_` table names. `rag_collections` records a unique name,
`model_id`, and `dims`. `rag_chunks` stores the chunk ID, text, and canonical JSON
metadata. Each collection has a `rag_vec_<integer collection id>` vec0 table
declared as `float[dims]`. Upserts validate every vector before starting the
transaction, then update the chunk and vector rows atomically.

Nearest-neighbor queries use sqlite-vec's L2 KNN operation. Results order by
distance and then chunk ID. The secondary order keeps repeated queries stable
when returned candidates have equal distances.

The file uses `PRAGMA user_version`, starting at version 1. A new version-0 file
migrates in one transaction. Opening fails without mutation when the file has a
newer version, unversioned `rag_` tables, or a collection whose vector table is
missing. An extension-load failure also closes the connection and fails open.
There is no scalar-search fallback.

The coordinator pins `sqlite-vec==0.1.9`. sqlite-vec is pre-1.0, and its Python
binding does not carry the stability promise of its SQL API. Dependency upgrades
therefore require the store tests on every supported operating system.

## Consequences

- Backups must copy `coordinator.db` and `rag.db`, including their WAL files when
  the coordinator is live.
- RAG failure cannot corrupt registry or queue tables.
- A collection cannot change embedding model or dimension in place. Operators
  create and reingest into a new collection instead.
- Python must use a SQLite build that supports loadable extensions. The packaged
  sqlite-vec binary does not remove that host-library requirement.
