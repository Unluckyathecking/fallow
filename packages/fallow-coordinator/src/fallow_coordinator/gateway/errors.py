"""OpenAI-style error responses.

Clients speak the OpenAI wire protocol, so every gateway-originated failure is
shaped as ``{"error": {"message": ..., "type": ...}}`` rather than FastAPI's
default ``{"detail": ...}``. ``type`` values mirror OpenAI's vocabulary plus the
gateway-specific ``no_replica_available`` used for the shed metric.
"""

from fastapi.responses import JSONResponse

TYPE_INVALID_REQUEST = "invalid_request_error"
TYPE_MODEL_NOT_FOUND = "model_not_found"
TYPE_NO_REPLICA = "no_replica_available"
TYPE_UPSTREAM = "upstream_error"


def openai_error(status_code: int, error_type: str, message: str) -> JSONResponse:
    """Build a JSON error body in the OpenAI ``{"error": {...}}`` envelope."""
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": error_type}},
    )
