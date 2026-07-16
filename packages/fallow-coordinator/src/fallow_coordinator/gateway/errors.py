"""OpenAI-style error responses.

Clients speak the OpenAI wire protocol, so every gateway-originated failure is
shaped as ``{"error": {"message": ..., "type": ...}}`` rather than FastAPI's
default ``{"detail": ...}``. ``type`` values mirror OpenAI's vocabulary plus the
gateway-specific ``no_replica_available`` used for the shed metric.
"""

import logging

from fastapi.responses import JSONResponse

TYPE_INVALID_REQUEST = "invalid_request_error"
TYPE_MODEL_NOT_FOUND = "model_not_found"
TYPE_NO_REPLICA = "no_replica_available"
TYPE_RATE_LIMIT = "rate_limit_error"
TYPE_UPSTREAM = "upstream_error"

_UPSTREAM_MESSAGE = "the upstream model server failed to serve the request"
_LOG_BODY_LIMIT = 2048  # cap the logged backend body so a huge trace can't flood the log

_logger = logging.getLogger(__name__)


def openai_error(
    status_code: int,
    error_type: str,
    message: str,
    *,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Build a JSON error body in the OpenAI ``{"error": {...}}`` envelope."""
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": error_type}},
        headers=headers,
    )


def sanitized_upstream_error(status_code: int, raw_body: bytes) -> JSONResponse:
    """Map a backend failure to a clean gateway error; log the raw body server-side.

    The backend (llama-server) can return a 5xx whose body is a raw exception
    string. That must never reach the client. The raw text is logged here for
    operators and the caller receives a generic 502 in the OpenAI envelope.
    """
    _logger.warning(
        "upstream returned %d: %s",
        status_code,
        raw_body[:_LOG_BODY_LIMIT].decode("utf-8", "replace"),
    )
    return openai_error(502, TYPE_UPSTREAM, _UPSTREAM_MESSAGE)
