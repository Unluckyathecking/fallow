"""Minimal, tolerant parsing of the inbound request body.

The gateway parses the JSON body **once** and reads only the fields it needs to
route (``model``), pick a transport (``stream``), and annotate the audit log
(``prompt_chars``). It deliberately does not validate anything else: llama-server
owns the semantics of chat/embedding requests, and the raw bytes are forwarded
verbatim. Every extractor is best-effort and never raises on odd input.
"""

import json
from dataclasses import dataclass
from typing import Any

_PROMPT_KEYS = ("input", "prompt")


@dataclass(frozen=True)
class ParsedBody:
    """The small routing and logging subset read from the request body."""

    model: str | None
    stream: bool
    prompt_chars: int | None
    first_user_message: str | None


def parse_body(raw: bytes) -> ParsedBody | None:
    """Parse ``raw`` once; return ``None`` if it is not a JSON object."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    model = data.get("model")
    return ParsedBody(
        model=model if isinstance(model, str) else None,
        stream=bool(data.get("stream", False)),
        prompt_chars=_prompt_chars(data),
        first_user_message=_first_user_message(data),
    )


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
