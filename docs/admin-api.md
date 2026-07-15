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
| POST | `/api_keys` | `{"name": str, "model_allowlist"?: [str], "rpm_limit"?: int, "daily_limit"?: int}` | 200/201 | `{"key": str}` |
| GET  | `/agents` | _(none)_ | 200 | `[AgentSnapshot]` |
| GET  | `/models` | _(none)_ | 200 | `[ModelManifest]` |
| POST | `/models` | `{"manifest": ModelManifest, "blob_path": str}` | 201 | _(empty)_ |
| PUT  | `/assignments` | `{"model_id": str, "agent_ids": [str]}` | 204 | _(empty)_ |
| POST | `/jobs` | `JobSubmit` | 200/201 | `JobStatus` |
| GET  | `/jobs/{job_id}` | _(none)_ | 200 | `JobStatus` |
| GET  | `/work_units/{unit_id}/payload` | _(none)_ | 200 | streamed bytes |

### Notes per route

- **`POST /enrollment_tokens`** — mints a single-use agent enrollment token
  (consumed by `RegisterRequest.enrollment_token`). No request body in v0.1.
- **`POST /api_keys`** — creates a client API key. `model_allowlist` omitted (or
  `null`) means "all models". `name` is a human label. The key is returned once,
  in clear, and stored hashed at rest (ADR 000 §6). `rpm_limit` and `daily_limit`
  are optional positive integers. Omitted or `null` values leave that limit
  unrestricted. Daily limits reset at 00:00 UTC; see [ADR 030](adr/030-api-key-quotas.md).
- **`GET /agents`** — returns the coordinator's current `AgentSnapshot` view
  (registration caps + latest heartbeat), one per enrolled agent.
- **`GET /models`** — returns every registered `ModelManifest`.
- **`POST /models`** — registers a model. `manifest` is a full `ModelManifest`
  (the CLI computes `sha256` + `size_bytes` by streaming the local blob).
  `blob_path` is a path **on the coordinator host**; v0.1 assumes the CLI runs
  there. Wave-3 ingests the blob at that path into the blob store.
- **`PUT /assignments`** — sets the *exact* set of agents assigned to serve a
  model (idempotent replace, not append). Drives `AgentConfig.assigned_models`.
- **`POST /jobs`** — submits a `JobSubmit`; the coordinator splits it into
  content-addressed work units (ADR 005) and returns the initial `JobStatus`.
- **`GET /jobs/{job_id}`** — returns the current `JobStatus`; unknown ids → 404.
- **`GET /work_units/{unit_id}/payload`** returns the payload attached to an
  accepted successful completion. It uses `application/octet-stream` and
  returns 404 when the unit is unknown, incomplete, failed, or its stored blob
  is missing.

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
