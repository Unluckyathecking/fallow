# ADR 043: Gateway request-body validation and upstream-error hygiene

**Status:** Accepted  
**Date:** 2026-07-16

## Context

The gateway forwards chat-completions bodies to llama-server verbatim and only
reads the fields it needs to route. That was fine until end-to-end testing hit the
live deployment with malformed bodies. Several fell through to the backend and came
back as 500 or 502, and one leaked a raw backend exception (`json.exception.out_of_range.403`)
straight to the client. A missing `messages` field, an empty `messages` list, `n`
other than 1, a negative `max_tokens`, and out-of-range `temperature`/`top_p` all
either surfaced a server error or were accepted silently.

Two things were wrong. The gateway did not validate client input at its boundary,
so a client mistake looked like a gateway fault. And a backend error body could reach
the client unchanged, which leaks internal detail and is a poor client contract.

## Decision

Validate the chat body at ingress, and sanitize any backend 5xx before it leaves the
gateway.

`validate_chat_body` in `bodyparse.py` checks a `POST /v1/chat/completions` body and
returns a typed `BodyError` (a client-safe message), never an exception. The contract:

- `messages` is a non-empty list, and every item is an object with `role` and `content`.
- `n` is absent or exactly 1 (the gateway serves one completion).
- `max_tokens` is absent or an integer `>= 1`. A negative value is rejected outright
  rather than silently clamped.
- `temperature` is absent or in `[0, 2]`; `top_p` is absent or in `[0, 1]`.

The service calls this once at ingress, after model resolution and before session
routing, admission, or any replica pick, and returns a 400 in the OpenAI error
envelope on failure. Other routes (embeddings) have a different schema and are left
to the backend, so the validator is a no-op off the chat path. The valid path is
unchanged: a well-formed request runs the same checks, all pass, and it proxies as
before.

`sanitized_upstream_error` in `errors.py` maps a backend failure to a generic 502 in
the OpenAI envelope and logs the raw backend body server-side (capped, and only there).
The buffered response path calls it whenever an upstream status is `>= 500`, which is
the single point where an exhausted-retry 5xx (buffered or streaming) lands. The raw
backend body never reaches the client.

## Consequences

- Every malformed case above returns a clean 400 with a clear message. No client
  mistake surfaces as a 500 or 502.
- No backend exception text can reach the client. Operators still see the raw body in
  the log for diagnosis.
- A sanitized upstream 5xx is recorded as `error` rather than `served`, which keeps the
  served-vs-shed metric honest.
- Validation is chat-specific. Embeddings and future routes need their own checks; the
  validator returns early for them today.
- Existing tests that posted a bare `{"model": ...}` chat body now send a minimal valid
  `messages` list, since such a body is malformed under this contract.
- All logic lives in `bodyparse.py` and `errors.py`; the service change is one localized
  call, so it rebases cleanly against other work that touches the same file.

## Verification

Unit tests assert each malformed case returns 400 with the `invalid_request_error`
envelope and that the response never contains a raw backend substring. One test drives a
backend 5xx with a leaky body and asserts the client sees a clean 502 while the raw text
appears only in the server log. Existing proxy, routing, streaming, admission, quota, and
auth tests pass with valid bodies, confirming the valid path is unchanged.
