# Coordinator Admin API (`/v1/admin/*`) — v0.1 contract

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
| POST | `/api_keys` | `{"name": str, "model_allowlist"?: [str]}` | 200/201 | `{"key": str}` |
| GET  | `/agents` | _(none)_ | 200 | `[AgentSnapshot]` |
| GET  | `/models` | _(none)_ | 200 | `[ModelManifest]` |
| POST | `/models` | `{"manifest": ModelManifest, "blob_path": str}` | 201 | _(empty)_ |
| PUT  | `/assignments` | `{"model_id": str, "agent_ids": [str]}` | 204 | _(empty)_ |
| POST | `/jobs` | `JobSubmit` | 200/201 | `JobStatus` |
| GET  | `/jobs/{job_id}` | _(none)_ | 200 | `JobStatus` |

### Notes per route

- **`POST /enrollment_tokens`** — mints a single-use agent enrollment token
  (consumed by `RegisterRequest.enrollment_token`). No request body in v0.1.
- **`POST /api_keys`** — creates a client API key. `model_allowlist` omitted (or
  `null`) means "all models". `name` is a human label. The key is returned once,
  in clear, and stored hashed at rest (ADR 000 §6).
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

## Open contract questions (for wave-3)

- Response envelope: routes return bare objects/arrays (not the common
  `{success,data,error}` envelope) to keep the wire shape equal to the
  `fallow_protocol` types. Revisit if a uniform envelope is wanted org-wide.
- Blob upload: v0.1 passes `blob_path` (co-located CLI). A multipart upload
  route (`POST /models` with the blob body) is the natural v0.2 extension for
  running `flw` off-host.
