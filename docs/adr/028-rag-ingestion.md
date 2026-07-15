# ADR 028: Fleet RAG ingestion

**Status:** accepted  
**Date:** 2026-07-15

## Context

Document ingestion needs embeddings from the managed fleet. The coordinator
already has a content-addressed chunker, durable embed jobs, unit lifecycle
state, and attempt-bound result payloads. A separate ingestion executor would
duplicate those guarantees and make retries harder to reason about.

E3.1's vector store is under review in PR #16. This change must remain testable
without importing its sqlite-vec implementation.

## Decision

The admin API accepts a model ID and pre-split document text chunks. It writes a
content-addressed JSONL corpus and calls the existing embed chunker. The
resulting work units run through the normal queue and embed worker.

The queue stores the target collection in job parameters. It also exposes a
coordinator-local job detail view with each unit's input reference, terminal
state, and accepted result reference. No new protocol type is added.

Ingestion readiness comes from `JobStatus`. A job is terminal only after every
unit is DONE or DEAD. The status route reports `partial` and the exact dead-unit
count when any work exhausted its retries. It never reports the collection as
ready in that case.

Finalization reads DONE unit inputs and result payloads in unit index order. It
checks the embed worker's `{embeddings, model_id, dims}` payload, then pairs rows
with the original strings. Chunk IDs are SHA-256 digests of the UTF-8 text. The
same upload therefore produces the same corpus, work-unit, and chunk IDs.
Repeated finalization uses vector upsert and cannot create duplicate chunk rows.

The app depends on a narrow `VectorSink` protocol. `create_app` accepts the sink
as an injected collaborator. Until PR #16 is integrated, production assembly
has no sink and the RAG ingestion routes return 503. Tests use an in-memory sink.

## Consequences

- Fleet retries, leases, payload binding, and dedup remain in one batch path.
- Clients poll one ingestion status resource. The state is derived from durable
  unit counts, not inferred from result-file presence.
- DONE units in a partial job are indexed. DEAD units remain visible in status
  and can be retried through a later upload.
- Uploaded input is an array of text chunks. File parsing and semantic chunk
  splitting remain outside this module.
- Integration after PR #16 must make its chunk value compatible with
  `IngestChunk` or provide a small adapter.
