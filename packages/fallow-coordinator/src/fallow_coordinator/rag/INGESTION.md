# RAG document ingestion

The ingestion path sends document chunks through the existing batch queue and
embed worker. It does not call an embedding model from the coordinator.

`POST /v1/admin/rag/collections/{collection}/documents` accepts a model ID and
an array of non-empty chunk strings. The coordinator writes a content-addressed
JSONL corpus, runs the standard embed chunker, and submits an `embed` job. The
response contains the ingestion ID and unit counts.

`GET /v1/admin/rag/collections/{collection}/ingestions/{id}` reads the durable
job state. A collection is ready only when every unit is DONE. If any unit is
DEAD, the response state is `partial` and includes `dead_units`.

When a job becomes terminal, the status call finalizes its DONE units. For each
unit it reads the original string array and the accepted result payload from the
coordinator result store. The payload must match the embed worker shape:
`{embeddings, model_id, dims}`. Row count, model ID, dimensions, finite values,
and the content-addressed payload reference are checked before vector upsert.

Chunk IDs are SHA-256 digests of UTF-8 text bytes. Reingesting the same text
therefore uses the same IDs and queue work-unit IDs. Finalization may run more
than once; the vector sink must treat an upsert of the same chunk ID as a
replacement, not a second row.

## Vector-store seam

E3.2 defines `VectorSink` with two async operations: `create_collection` and
`upsert`. The production app gives the ingestion service its shared
`RagVectorStore`. A test can pass `create_app(vector_sink=...)` to replace only
the ingestion side without changing query storage.
