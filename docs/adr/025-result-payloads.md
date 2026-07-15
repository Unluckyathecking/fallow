# ADR 025: Attempt-bound result payloads

Status: accepted · Date: 2026-07-15

## Context

Batch workers produce payloads that were previously left on the agent. A result
reference in `WorkResult` was not enough to retrieve those bytes from the
coordinator. It also left two races open. An agent could upload while its lease
was being reassigned, and a late completion could refer to a payload from an
older attempt.

The payload may be large, so the coordinator must not buffer the whole request.
Retries are normal when agents disappear or leases expire.

## Decision

The coordinator stores result bytes under their lowercase SHA-256 digest in a
dedicated `result_dir`. Uploads are streamed in 1 MiB chunks, capped by
`max_result_payload_bytes`, and published from a temporary file in the same
directory. Existing coordinator configs may omit `result_dir`; the coordinator
then uses a `results` directory beside `db_path`. Repeating an existing digest
is safe and does not rewrite the blob.

Both payload upload and completion carry `X-Fallow-Lease-Attempt`. The upload
route checks the lease before reading the body, then checks it again before it
records a binding. The binding contains the work unit, agent, attempt, digest,
result reference, and acceptance time. A successful completion must match the
current lease and its binding. Failed completion still requires the current
lease, but it does not require a payload.

The admin payload route resolves the reference from an accepted successful
completion. It never takes a digest from the request path. Missing, incomplete,
failed, and unbound units return 404.

Before an agent uploads, it writes an attempt-specific retry copy locally. It
removes that copy only after the coordinator returns the digest the agent
computed. Transport errors, server errors, and digest mismatches retry with
bounded exponential backoff while lease slack remains. Any final upload or local
persistence error leaves the lease uncompleted, so normal expiry and requeue
rules decide the next attempt.

## Consequences

- A stale or reassigned attempt cannot publish a completion.
- Duplicate bytes and exact duplicate completions are idempotent.
- The coordinator uses bounded memory while receiving payloads.
- A lease that changes after the blob is published but before the binding is
  recorded can leave an unreferenced blob. This is safer than recording a
  reference before the bytes exist. Garbage collection is deferred.
- Retried work is recomputed today. Reusing the saved local payload can be added
  later without changing the coordinator contract.

## Verification

Queue tests cover matching, stale, conflicting, and duplicate bindings. Route
tests cover auth, size rejection, completion validation, and admin visibility.
The integration batch scenario uploads real bytes, completes the units, and
retrieves the same bytes from the coordinator.
