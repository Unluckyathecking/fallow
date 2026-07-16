"""A cheap, explainable verdict on where a request *could* have run.

This is measurement, not routing. Today the client names the ``model`` and the
gateway just places it; to route intelligently later ("this one can run local,
that one should escalate to a frontier API") we first need to know how many
requests were even candidates for local placement. :func:`classify_eligibility`
produces that per-request verdict from signals already sitting in the parsed
body — no model call, no SSE parse, negligible cost. Nothing in this package
acts on the verdict; it is logged only. See ADR 050.

Each signal earns its place:

* **Prompt size** (chars ÷ 4 ≈ tokens): a small prompt is the bread-and-butter a
  local replica serves well; a very large context is where a small model's
  quality and context window give out.
* **Code fences** (``` ``` ```): a triple-backtick block marks a real coding
  task. Paired with any real size it is the multi-file code+reasoning work least
  suited to a small local model.
* **Message count**: a long back-and-forth carries a lot of accumulated context,
  which pushes the same way as raw length.
* **Model tier**: if the client already aimed at a large model (a ``…70b`` in
  the id), take that at face value — they asked for heavy compute.

The thresholds are deliberately coarse. The point is an honest denominator, so
the wide middle band is called ``unknown`` rather than guessed.
"""

from enum import StrEnum
from typing import Any

_CHARS_PER_TOKEN = 4  # rough English/code average; only the order of magnitude matters
_SMALL_TOKENS = 256  # a sentence or two — a label/classify/one-liner ask
_LARGE_TOKENS = 2000  # a long document or multi-file paste
_CODE_TOKENS = 800  # code present *and* already sizeable — a genuine coding task
_FEW_MESSAGES = 2  # a single user turn, optionally with a system prompt
_MANY_MESSAGES = 8  # a long thread carrying accumulated context
_LARGE_MODEL_PARAMS_B = 30  # a "…70b"-class id is a heavy-compute request

_CODE_FENCE = "```"
_PROMPT_KEYS = ("input", "prompt")


class Eligibility(StrEnum):
    """Where a request could have run, judged from the request alone."""

    LOCAL_OK = "local_ok"  # small, single-shot, no code — a local replica handles it
    ESCALATE = "escalate"  # big context / multi-file code / heavy model — wants a frontier API
    UNKNOWN = "unknown"  # the honest middle band we decline to guess


def classify_eligibility(body: dict[str, Any]) -> Eligibility:
    """Return a cheap eligibility verdict for one parsed request body."""
    tokens = _content_chars(body) // _CHARS_PER_TOKEN
    has_code = _has_code_fence(body)
    messages = _message_count(body)

    if _is_large_model(body.get("model")):
        return Eligibility.ESCALATE
    if (
        tokens >= _LARGE_TOKENS
        or (has_code and tokens >= _CODE_TOKENS)
        or messages >= _MANY_MESSAGES
    ):
        return Eligibility.ESCALATE
    if tokens <= _SMALL_TOKENS and not has_code and messages <= _FEW_MESSAGES:
        return Eligibility.LOCAL_OK
    return Eligibility.UNKNOWN


def _content_chars(body: dict[str, Any]) -> int:
    """Total characters across chat messages and embedding-style prompt fields."""
    total = 0
    for content in _iter_contents(body):
        total += len(content)
    return total


def _has_code_fence(body: dict[str, Any]) -> bool:
    return any(_CODE_FENCE in content for content in _iter_contents(body))


def _message_count(body: dict[str, Any]) -> int:
    messages = body.get("messages")
    return len(messages) if isinstance(messages, list) else 0


def _iter_contents(body: dict[str, Any]) -> list[str]:
    """Every string the client sent as prompt content, best-effort and tolerant."""
    out: list[str] = []
    messages = body.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                out.append(message["content"])
    for key in _PROMPT_KEYS:
        value = body.get(key)
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, list):
            out.extend(item for item in value if isinstance(item, str))
    return out


def _is_large_model(model: Any) -> bool:
    """True when the model id advertises a large parameter count (e.g. ``…70b``)."""
    if not isinstance(model, str):
        return False
    return _max_param_billions(model) >= _LARGE_MODEL_PARAMS_B


def _max_param_billions(model: str) -> int:
    """Largest ``<n>b`` parameter hint in the id, or 0 when none is discernible."""
    best = 0
    digits = ""
    for char in model.lower():
        if char.isdigit():
            digits += char
        elif char == "b" and digits:
            best = max(best, int(digits))
            digits = ""
        else:
            digits = ""
    return best
