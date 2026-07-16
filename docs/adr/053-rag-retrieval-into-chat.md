# ADR 053: RAG retrieval into chat generation

**Status:** accepted

**Date:** 2026-07-16

## Context

The RAG query route (ADR 034) lets a client retrieve chunks, but grounding a
chat still takes two round trips: query the collection, then paste the results
into a prompt by hand. A small local model is far more useful on internal
knowledge when that step is automatic.

The gateway forwards chat bodies verbatim and streams the reply untouched (ADR
012). Retrieval must not disturb that. The gateway and RAG packages are sibling
layers in the import graph, so the gateway cannot reach into the RAG package for
the embed-and-search path — the same boundary ADR 034 worked around.

## Decision

A chat body gains an optional `rag` object, `{"collection": str, "k": int}`. When
it is present the gateway embeds the last user message, searches the collection,
and prepends the retrieved chunks to `messages` as one system message before
proxying. The `rag` field is removed from the forwarded body, so the replica sees
an ordinary chat request. `k` is bounded to 1 through 64; an unknown collection
returns 404 and a collection with no healthy embedding replica returns 503, all
in the gateway's OpenAI-style error envelope. When `rag` is absent the body is
forwarded byte for byte.

Retrieval runs once, before the upstream call is made, so the streaming hot path
stays a verbatim passthrough — nothing new happens inside the stream.

The embed-and-search steps that ADR 034's query route already performs are
extracted into `rag/retrieval.py` and shared by both callers, so there is one
retrieval path rather than two. The gateway never imports that module: the app
layer wires a small retriever closure into the gateway and translates the RAG
package's `RetrievalError` into the gateway error envelope, keeping the sibling
boundary intact. The prompt-shaping — parsing `rag`, bounding `k`, and building
the context message — lives in a gateway helper so `service.py` stays small.

The embedding replica is chosen through the scheduler pick the gateway already
uses (inflight-enriched, policy-delegating), not a blind first endpoint, and a
connect failure or non-200 is retried once on a different replica before any byte
is committed — the same before-first-byte guarantee the chat proxy gives. The app
layer builds that picker once and hands it to the gateway, the query route, and
the retriever closure, so the pick path is shared rather than reimplemented and
the gateway↔RAG boundary is untouched (retrieval declares its own picker alias).

The context message is a fixed template with a documented trust boundary. The
retrieved chunks are indexed documents, not a trusted operator, but they sit in a
`system` message. To stop a chunk from borrowing the system role's authority — a
prompt-injection vector — the chunks are wrapped in explicit `UNTRUSTED CONTEXT`
markers and the preamble tells the model to treat them as data and never follow
instructions inside them. The marker sentinels are stripped from each chunk before
assembly, so a chunk cannot forge an `END` marker to close the fence early; the
delimiter therefore can't be broken, and the framing instruction carries the
semantic boundary. This stays a mitigation, not a guarantee — a model can still be
swayed by the content itself, just not by breaking the delimiter. Each request's
`GatewayLogEntry` records `rag_k`, the number of chunks folded into the prompt,
and null when retrieval was not requested.

## Consequences

- One chat call can ground itself on a collection with no separate query round
  trip, which is what makes a small on-prem model good enough on internal
  questions.
- Requests that omit `rag` are unchanged, so the streaming and buffered paths and
  their tests are unaffected.
- `rag_k` gives the request log a direct measure of how often grounding is used
  and how many chunks it pulls in.
- The retriever reuses the first-healthy embedding endpoint from ADR 034, so it
  inherits that policy's simplicity and its lack of load-aware selection or
  retry.
- The gateway depends on an injected retriever, not on the RAG package, so the
  two layers can still move independently.
- The context wrapper reduces but does not eliminate prompt-injection risk from
  indexed documents; operators should keep collections curated and not rely on
  retrieved context for authorization or safety-critical decisions.

## Follow-ups (not yet addressed)

- The assembled context is bounded only by chunk count (`k` ≤ 64), not by an
  aggregate byte or token budget, so a few large chunks can still produce a very
  large prompt. Eligibility telemetry now classifies the augmented prompt, which
  will show whether this matters before a bound is added.
- Retrieval runs before admission, so a request that will ultimately be shed can
  still spend the full embedding timeout first. Ordering retrieval after
  admission is a later refinement.
