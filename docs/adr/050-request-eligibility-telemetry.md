# ADR 050: Request-eligibility telemetry (measure before routing)

**Status:** Accepted  
**Date:** 2026-07-15

## Context

Today the client names the `model` and the gateway just places it. To ever route
intelligently — keep a cheap request on a local replica, send a heavy one to a
frontier API — we would first need to know how many requests could even have gone
local. We do not have that number. Building a routing policy before we have it
would be guessing at the size of the prize.

So this is the measurement, and only the measurement. It adds a per-request verdict
— *could this have run local?* — to the existing gateway log, computed from signals
already in the parsed body. Nothing routes on it. It exists to give the eventual
routing work an honest denominator instead of an assumption.

## Decision

Add `classify_eligibility(body) -> Eligibility` in a new gateway-local module
`eligibility.py`. It is a pure function over the already-parsed request dict — no
model call, no SSE parse, no second body read — returning one of three verdicts:

- `local_ok` — small, single-shot, no code. The bread-and-butter a local replica
  serves well.
- `escalate` — a large context, sizeable multi-file code, a long thread, or a model
  id that already advertises a large parameter count. The work a small model is
  least likely to handle well.
- `unknown` — the wide middle band we decline to guess. This is deliberate: a made-up
  verdict here would poison the denominator we are trying to measure.

The signals, and why each earns its place:

- **Prompt size** (content chars ÷ 4 ≈ tokens). The cheapest proxy for difficulty.
  A short prompt leans `local_ok`; a very large context leans `escalate`.
- **Code fences** (` ``` `). A triple-backtick block marks a real coding task; paired
  with any real size it is the multi-file code+reasoning work that wants a stronger
  model.
- **Message count**. A long back-and-forth carries accumulated context, which pushes
  the same way as raw length.
- **Model tier**. If the id carries a large parameter hint (a `…70b`), take the client
  at their word — they asked for heavy compute.

Thresholds are coarse on purpose (256 tokens small, 2000 large, 800 for sized code,
8 messages for a long thread, 30B for a large model). The point is not a precise
score, it is a defensible bucketing, and the honest middle stays `unknown`.

The verdict is logged, not acted on. `GatewayLogEntry` gains one optional field,
`eligibility: str | None`. It is a gateway-local log model, not a protocol wire type,
so `schemas/` is untouched.

The whole thing is opt-in behind `GatewayConfig.eligibility_telemetry`, default off.
Off means the classifier is never called, the field stays `null`, and the served path
is byte-for-byte what it was. The service computes the verdict once, right after
parsing, and threads it through to the single log-emit point exactly as `affinity` is
threaded today. The streaming hot path is unchanged.

## Consequences

- We start collecting the real distribution of `local_ok` / `escalate` / `unknown`
  across live traffic, per request, at negligible cost.
- No behaviour changes. No routing decision is made from the verdict in this change,
  and with the flag off there is no field and no classification at all.
- The routing brain that acts on this is a deliberate follow-up. It inherits a measured
  denominator instead of an assumed one.
- The heuristic is intentionally simple and will misclassify at the margins. That is
  acceptable for a measurement whose middle band is explicitly `unknown`; the thresholds
  can be retuned from the very data this collects before any policy leans on them.
- The classifier is self-contained and pure, so it is trivial to unit-test and cheap to
  revisit. It does not import from the routing or streaming paths.

## Verification

Unit tests pin the documented mapping: a short classification prompt → `local_ok`, a long
multi-file code+reasoning prompt → `escalate`, a mid-size no-code prompt and a tiny code
snippet → `unknown`, a long thread and a `…70b` model id → `escalate`, and a short
embedding input → `local_ok`. Integration tests through the gateway assert that with the
flag off the served path is byte-for-byte unchanged and the field is `null`, and that with
it on the verdict is attached on both the served and the shed paths. The existing gateway,
proxy, routing, streaming, and log tests pass unchanged, and the schema-export test confirms
no wire drift.
