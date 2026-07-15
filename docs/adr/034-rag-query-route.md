# ADR 034: RAG query route

**Status:** accepted  
**Date:** 2026-07-15

## Context

A collection records the embedding model and dimension used for its chunks.
Querying it requires one fresh embedding from that same model before the vector
store can rank candidates. The RAG and gateway packages are sibling layers, so
the query route cannot borrow the gateway's replica picker without breaking the
import boundary.

API-key holders can call this route. It must apply the model allowlist already
attached to each key before sending a query to an embedding replica.

## Decision

`POST /v1/rag/collections/{collection}/query` accepts a non-empty query and a
positive result count. It authenticates through `SqliteRegistry`, looks up the
collection, and checks the key's model allowlist against the collection model.

The route asks `registry.replica_endpoints(model_id, now)` for healthy
replicas and calls the first endpoint's OpenAI-compatible embeddings API. This
small policy remains separate from gateway routing. Each query needs one short
embedding call. Load-aware selection and retry can wait until measurements
show a need for them.

The query route accepts one non-empty row of finite numbers from the embedding
replica. `RagVectorStore` then performs its existing L2 nearest-neighbor query.
The API returns the stored chunk ID, text, complete metadata object, and
sqlite-vec L2 distance as `score`. Lower scores are better. Keeping the raw
distance avoids a similarity conversion whose meaning would vary by embedding
model.

The coordinator owns one `RagVectorStore` beside `coordinator.db`. The same
instance receives completed ingestion chunks and serves queries. The app opens
and closes it with the coordinator lifespan.

## Consequences

- A key restricted to other models receives 403 before any embedding call.
- A collection with no healthy embedding replica returns 503 with the model ID.
- Replica transport failures and invalid embedding responses return 502.
- The first-healthy rule is deterministic, but it does not balance embedding
  calls or retry another replica.
- Clients must treat `score` as a distance. Comparing values across collections
  or embedding models is not meaningful.
