"""Minimal, tolerant parsing of the inbound request body.

The gateway parses the JSON body **once** and reads only the fields it needs to
route (``model``), pick a transport (``stream``), and annotate the audit log
(``prompt_chars``). It deliberately does not validate anything else: llama-server
owns the semantics of chat/embedding requests, and the raw bytes are forwarded
verbatim. Every extractor is best-effort and never raises on odd input.

One exception is the chat-completions body. Malformed chat requests used to fall
through to the backend and surface as 500/502 (sometimes leaking a raw backend
exception string). :func:`validate_chat_body` rejects the known-bad shapes at the
gateway boundary with a clear message, so the client always sees a clean 400.
"""

import json
from dataclasses import dataclass
from typing import Any

_PROMPT_KEYS = ("input", "prompt")
_CHAT_COMPLETIONS_PATH = "/v1/chat/completions"


@dataclass(frozen=True)
class ParsedBody:
    """The small routing and logging subset read from the request body."""

    model: str | None
    stream: bool
    prompt_chars: int | None
    first_user_message: str | None


def parse_json_object(raw: bytes) -> dict[str, Any] | None:
    """Decode ``raw`` into a JSON object, or ``None`` if it is not one."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def parsed_body(data: dict[str, Any]) -> ParsedBody:
    """Read the routing and logging subset from an already-decoded object."""
    model = data.get("model")
    return ParsedBody(
        model=model if isinstance(model, str) else None,
        stream=bool(data.get("stream", False)),
        prompt_chars=_prompt_chars(data),
        first_user_message=_first_user_message(data),
    )


def parse_body(raw: bytes) -> ParsedBody | None:
    """Parse ``raw`` once; return ``None`` if it is not a JSON object."""
    data = parse_json_object(raw)
    return parsed_body(data) if data is not None else None


@dataclass(frozen=True)
class BodyError:
    """A boundary-validation failure, carrying a client-safe message."""

    message: str


def validate_chat_body(path: str, data: dict[str, Any]) -> BodyError | None:
    """Validate an already-parsed chat body; return ``None`` when acceptable.

    Only ``POST /v1/chat/completions`` is checked — other routes (embeddings)
    have different schemas and are left to the backend.
    """
    if path != _CHAT_COMPLETIONS_PATH:
        return None
    return (
        _check_messages(data.get("messages"))
        or _check_n(data.get("n"))
        or _check_min_int("max_tokens", data.get("max_tokens"), minimum=1)
        or _check_range("temperature", data.get("temperature"), 0.0, 2.0)
        or _check_range("top_p", data.get("top_p"), 0.0, 1.0)
    )


def _check_messages(value: Any) -> BodyError | None:
    if not isinstance(value, list) or not value:
        return BodyError("'messages' must be a non-empty list")
    for message in value:
        if not isinstance(message, dict) or "role" not in message or "content" not in message:
            return BodyError("each item in 'messages' must have 'role' and 'content'")
    return None


def _check_n(value: Any) -> BodyError | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value != 1:
        return BodyError("'n' must be 1 — only one completion choice is supported")
    return None


def _check_min_int(field: str, value: Any, *, minimum: int) -> BodyError | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        return BodyError(f"'{field}' must be an integer >= {minimum}")
    return None


def _check_range(field: str, value: Any, low: float, high: float) -> BodyError | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return BodyError(f"'{field}' must be a number in [{low}, {high}]")
    if not low <= value <= high:
        return BodyError(f"'{field}' must be in [{low}, {high}]")
    return None


def _first_user_message(data: dict[str, Any]) -> str | None:
    messages = data.get("messages")
    if not isinstance(messages, list):
        return None
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
    return None


def _prompt_chars(data: dict[str, Any]) -> int | None:
    """Best-effort character count of the prompt across chat/embedding shapes."""
    total = 0
    found = False
    messages = data.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                total += len(message["content"])
                found = True
    for key in _PROMPT_KEYS:
        value = data.get(key)
        if isinstance(value, str):
            total += len(value)
            found = True
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    total += len(item)
                    found = True
    return total if found else None
