"""Opt-in retrieval-into-generation for the chat route.

A chat body may carry ``"rag": {"collection": str, "k": int}``. When it does, the
gateway retrieves the nearest chunks for the last user message and prepends them
to the prompt as one system message, then drops the ``rag`` field and proxies the
rewritten body as normal. Retrieval runs here, *before* the upstream call, so the
streaming hot path stays a verbatim passthrough.

The retriever itself is injected (:data:`ChunkRetriever`) so the gateway never
imports the RAG package — the two are dependency-graph siblings, wired together
by the app layer.
"""

from __future__ import annotations

import copy
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

# The injected retrieval seam: embed the query, search ``collection``, return the
# text of the ``k`` nearest chunks. Raises :class:`RagRetrievalError` on failure.
ChunkRetriever = Callable[[str, str, int], Awaitable[tuple[str, ...]]]

_MAX_K = 64
_CONTEXT_PREAMBLE = (
    "Use the following retrieved context to answer the user's question. "
    "If it is not relevant, rely on your own knowledge and ignore it."
)


class RagRetrievalError(Exception):
    """A retrieval-into-chat failure carrying its OpenAI-envelope wire shape."""

    def __init__(self, status_code: int, error_type: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type
        self.message = message


@dataclass(frozen=True)
class RagResult:
    """The rewritten chat body plus the count of chunks folded into the prompt."""

    body: dict[str, Any]
    rag_k: int


async def apply_rag(data: dict[str, Any], retriever: ChunkRetriever | None) -> RagResult | None:
    """Return the rewritten body if ``data`` requested RAG, else ``None``.

    ``data`` is never mutated; the rewritten copy carries the prepended context
    message and no ``rag`` field.
    """
    if "rag" not in data:
        return None
    if retriever is None:
        raise RagRetrievalError(400, "invalid_request_error", "rag retrieval is not enabled")
    collection, k = _parse_spec(data["rag"])
    query = _last_user_message(data)
    if query is None:
        raise RagRetrievalError(
            400, "invalid_request_error", "rag requires a user message to retrieve for"
        )
    chunks = await retriever(collection, query, k)
    body = copy.deepcopy(data)
    del body["rag"]
    if chunks:
        body["messages"] = [_context_message(chunks), *body.get("messages", [])]
    return RagResult(body=body, rag_k=len(chunks))


def _parse_spec(spec: Any) -> tuple[str, int]:
    if not isinstance(spec, dict):
        raise RagRetrievalError(422, "invalid_request_error", "'rag' must be an object")
    collection = spec.get("collection")
    if not isinstance(collection, str) or not collection:
        raise RagRetrievalError(
            422, "invalid_request_error", "'rag.collection' must be a non-empty string"
        )
    k = spec.get("k")
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise RagRetrievalError(
            422, "invalid_request_error", "'rag.k' must be an integer >= 1"
        )
    if k > _MAX_K:
        raise RagRetrievalError(422, "invalid_request_error", f"'rag.k' must not exceed {_MAX_K}")
    return collection, k


def _last_user_message(data: dict[str, Any]) -> str | None:
    messages = data.get("messages")
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content
    return None


def _context_message(chunks: tuple[str, ...]) -> dict[str, str]:
    numbered = "\n\n".join(f"[{index}] {text}" for index, text in enumerate(chunks, start=1))
    return {"role": "system", "content": f"{_CONTEXT_PREAMBLE}\n\n{numbered}"}
