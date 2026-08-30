# Coordinator Admin API (`/v1/admin/*`)

This document is the **contract** for the coordinator's admin HTTP API. The
`flw` CLI (module L1) is built against it today; wave-3 assembles the coordinator
app and implements the server side from this spec. Keep it minimal and RESTful.

## Conventions

- **Base path**: all routes below are prefixed with `/v1/admin`.
- **Transport**: HTTP + JSON, inside the tailnet (ADR 000 §6). No mTLS in v0.1.
- **Auth**: every route requires `Authorization: Bearer <admin key>`. A missing
  or unknown key returns **401**; a key lacking admin scope returns **403**. The
  CLI renders both as `admin key rejected` and exits `2`.
- **Wire types**: request/response JSON references `fallow_protocol` wire types
  by their pydantic field names (all frozen, `extra="forbid"`). Small bodies
  unique to admin operations are defined in `fallow_cli.models` and duplicated in
  the table below.
- **Errors**: non-2xx responses SHOULD carry `{"detail": "<message>"}` (FastAPI
  default). The CLI surfaces `detail`/`error` if present, else the status code.

## Routes

| Method | Path | Request body | Success | Response body |
|--------|------|--------------|---------|---------------|
| POST | `/enrollment_tokens` | _(none)_ | 200/201 | `{"token": str}` |
| GET  | `/enrollment_tokens` | _(none)_ | 200 | `[{"token_id": str, "mode": str, "state": str, "created_at": str}]` |
| DELETE | `/enrollment_tokens/{token_id}` | _(none)_ | 204 | _(empty)_ |
| POST | `/agents/{agent_id}/revoke` | _(none)_ | 204 | _(empty)_ |
| POST | `/api_keys` | `{"name": str, "model_allowlist"?: [str], "rpm_limit"?: int, "daily_limit"?: int}` | 200/201 | `{"key": str}` |
| GET  | `/agents` | _(none)_ | 200 | `[AgentSnapshot]` |
| GET  | `/agents/revoked` | _(none)_ | 200 | `[{"agent_id": str, "hostname": str, "revoked_at": str}]` |
| GET  | `/models` | _(none)_ | 200 | `[ModelManifest]` |
| POST | `/models` | `{"manifest": ModelManifest, "blob_path": str}` | 201 | _(empty)_ |
| PUT  | `/assignments` | `{"model_id": str, "agent_ids": [str]}` | 204 | _(empty)_ |
| POST | `/assignments/fit` | `{"model_id": str}` | 200 | `{"model_id", "assigned", "kept", "skipped", "offline"}` |
| POST | `/jobs` | `JobSubmit` | 200/201 | `JobStatus` |
| GET  | `/jobs/{job_id}` | _(none)_ | 200 | `JobStatus` |
| GET  | `/jobs/{job_id}/units` | _(none)_ | 200 | `{"job_id", "model_id", "units": [{"idx", "work_unit_id", "state", "result_status", "result_ref"}]}` |
| GET  | `/work_units/{unit_id}/payload` | _(none)_ | 200 | streamed bytes |
| POST | `/rag/collections/{collection}/documents` | `{"model_id": str, "chunks": [str]}` | 202 | `IngestionStatus` |
| GET | `/rag/collections/{collection}/ingestions/{id}` | _(none)_ | 200 | `IngestionStatus` |

### Notes per route

- **`POST /enrollment_tokens`** — mints a single-use agent enrollment token
  (consumed by `RegisterRequest.enrollment_token`). No request body in v0.1.
- **`POST /api_keys`** — creates a client API key. `model_allowlist` omitted (or
  `null`) means "all models". `name` is a human label. The key is returned once,
  in clear, and stored hashed at rest (ADR 000 §6). `rpm_limit` and `daily_limit`
  are optional positive integers. Omitted or `null` values leave that limit
  unrestricted. Daily limits reset at 00:00 UTC; see [ADR 030](adr/030-api-key-quotas.md).
- **`GET /enrollment_tokens`** — lists every minted enrollment token by its
  **token id** (the first 12 hex characters of the sha256 the coordinator
  stores), never the token itself. `state` is `outstanding`, `used` (an agent
  enrolled with it) or `revoked`. `flw site join-bundles` prints the same id
  beside each join file it writes, so a desk can be mapped to a token without
  holding one.
- **`DELETE /enrollment_tokens/{token_id}`** — voids an unused enrollment token.
  A join file carrying it then fails enrollment exactly as a re-used one does.
  Unknown or already-spent ids return **404**; there is nothing to undo. The id
  is matched case-insensitively and must be exactly 12 hex characters; a
  malformed id, or one that would name more than one outstanding token, is its
  own **404** with a detail saying so rather than reading as "already spent".
- **`POST /agents/{agent_id}/revoke`** — revokes an enrolled agent's device
  token. Every later call presenting it returns **401**, the agent leaves
  `GET /agents` and interactive routing at once, its assignments are cleared and
  its in-flight relay work is dropped. Idempotent; unknown ids return **404**.
  There is **no un-revoke**: a wiped machine re-enrolls from a fresh token as a
  new agent id, which is the same path a reimaged desk already takes.
- **`GET /agents`** — returns the coordinator's current `AgentSnapshot` view
  (registration caps + latest heartbeat), one per enrolled agent. Revoked agents
  are absent; `GET /agents/revoked` is where they are.
- **`GET /agents/revoked`** — lists every revoked agent, oldest revocation
  first. It is the only place a revoked row is visible outside Site Mode's fleet
  view, and it is what tells a revoked desk apart from one that never enrolled.
  `flw agents list --revoked` prints it.
- **`GET /models`** — returns every registered `ModelManifest`.
- **`POST /models`** — registers a model. `manifest` is a full `ModelManifest`
  (the CLI computes `sha256` + `size_bytes` by streaming the local blob).
  `blob_path` is a path **on the coordinator host**; v0.1 assumes the CLI runs
  there. Wave-3 ingests the blob at that path into the blob store. Returns 422
  if `blob_path` is not a file, or if the manifest declares an `mmproj`
  companion whose file is not present beside the blob.
- **`PUT /assignments`** — sets the *exact* set of agents assigned to serve a
  model (idempotent replace, not append). Drives `AgentConfig.assigned_models`.
- **`POST /assignments/fit`** — one operator-triggered sweep: assigns the model
  to every live agent that has no assignment yet and can hold it (same fit gate
  as `PUT /assignments`). Agents already serving the model come back as `kept`;
  agents holding another model or failing the fit come back as `skipped` with a
  reason; agents with no recent heartbeat come back as `offline` and are never
  written to. Unknown model → 404. Exposed as `flw assign <model-id> --fit`.
- **`POST /jobs`** — submits a `JobSubmit`; the coordinator splits it into
  content-addressed work units (ADR 005) and returns the initial `JobStatus`.
- **`GET /jobs/{job_id}`** — returns the current `JobStatus`; unknown ids → 404.
- **`GET /jobs/{job_id}/units`** — the job's units in `idx` order with their
  ids, states, and result refs, so completed payloads can be downloaded
  through the payload route. Exposed as `flw jobs units` and driven by
  `flw jobs fetch --out <dir>`. For OCR jobs, join fetched payloads back to
  `corpus.json` by the `page` field inside each result document — `idx`
  follows the chunker's sorted content-hashed filenames, not corpus order.
- **`GET /work_units/{unit_id}/payload`** returns the payload attached to an
  accepted successful completion. It uses `application/octet-stream` and
  returns 404 when the unit is unknown, incomplete, failed, or its stored blob
  is missing.
- **`POST /rag/collections/{collection}/documents`** submits non-empty text
  chunks for fleet embedding. The route returns an ingestion ID and durable job
  counts. The chunks are content-addressed before queue submission.
- **`GET /rag/collections/{collection}/ingestions/{id}`** returns `running`,
  `ready`, or `partial` with `total_units`, `done_units`, `dead_units`, and
  `indexed_chunks`. A terminal status also performs idempotent vector upsert for
  completed units.

## Result payload flow

Agents upload result bytes before they complete a unit:

1. `POST /v1/agents/{agent_id}/work_units/{unit_id}/payload` sends the raw body
   with device bearer auth and `X-Fallow-Lease-Attempt`.
2. The coordinator streams the body into its content-addressed result store and
   returns `{"result_ref": "<sha256>"}`. Uploads larger than
   `max_result_payload_bytes` return 413.
3. The agent sends the same attempt header and reference to
   `POST /v1/agents/{agent_id}/work_units/{unit_id}/result`.

The coordinator checks the lease before and after streaming. It records the
unit, agent, attempt, and digest together, then accepts a successful completion
only when its reference matches that record. A stale attempt or conflicting
reference returns 409. Repeating the same upload and completion is safe.

## Open contract questions (for wave-3)

- Response envelope: routes return bare objects/arrays (not the common
  `{success,data,error}` envelope) to keep the wire shape equal to the
  `fallow_protocol` types. Revisit if a uniform envelope is wanted org-wide.
- Blob upload: v0.1 passes `blob_path` (co-located CLI). A multipart upload
  route (`POST /models` with the blob body) is the natural v0.2 extension for
  running `flw` off-host.
