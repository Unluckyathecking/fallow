"""Frozen tuning knobs for the coordinator client and event sink.

Both are immutable so a single instance can be shared across tasks without any
caller mutating another's retry/backoff behaviour.
"""

from pydantic import BaseModel, ConfigDict, Field

from fallow_agent.heartbeat.constants import (
    DEFAULT_CLIENT_BACKOFF_S,
    DEFAULT_CLIENT_MAX_RETRIES,
    DEFAULT_EVENT_BACKOFF_S,
    DEFAULT_MAX_PUSH_ATTEMPTS,
)


class ClientRetryConfig(BaseModel):
    """Retry policy for idempotent coordinator calls (heartbeat, poll_work)."""

    model_config = ConfigDict(frozen=True)

    max_retries: int = Field(default=DEFAULT_CLIENT_MAX_RETRIES, ge=0)
    backoff_base_s: float = Field(default=DEFAULT_CLIENT_BACKOFF_S, gt=0)


class EventSinkConfig(BaseModel):
    """Push policy for :class:`HttpEventSink`.

    ``max_push_attempts`` bounds how many times the background sender tries to
    POST one event before dropping it (the local JSONL keeps the durable copy).
    """

    model_config = ConfigDict(frozen=True)

    max_push_attempts: int = Field(default=DEFAULT_MAX_PUSH_ATTEMPTS, ge=1)
    backoff_base_s: float = Field(default=DEFAULT_EVENT_BACKOFF_S, gt=0)
