"""The gateway request-log record — the experiment's interactive dataset.

One :class:`GatewayLogEntry` is emitted per inbound interactive request. The
``status`` field carries the metric that matters most for the study: the ratio
of ``served`` to ``shed`` requests is the "% served on-prem" figure. Timestamps
are captured from the injected clock so the record is deterministic under test.
"""

from datetime import datetime
from enum import StrEnum

from fallow_protocol.base import FallowModel


class LogStatus(StrEnum):
    """Terminal disposition of one interactive request."""

    SERVED = "served"  # a replica produced the response bytes (any backend status)
    SHED = "shed"  # no replica was available; request bounced with 503
    ERROR = "error"  # no replica could be reached after retry; gateway returned 502
    CANCELLED = "cancelled"  # caller disconnected while waiting for admission


class AffinityState(StrEnum):
    """Session-affinity state observed while routing a request."""

    HIT = "hit"
    MISS = "miss"
    NONE = "none"


class GatewayLogEntry(FallowModel):
    """Immutable audit record for a single interactive request."""

    client_key_name: str
    model_id: str
    agent_id: str | None = None
    t_submit: datetime
    t_first_byte: datetime | None = None
    t_done: datetime
    status: LogStatus
    retried: bool = False
    prompt_chars: int | None = None
    waited_ms: int = 0
    affinity: AffinityState = AffinityState.NONE
    rag_k: int | None = None  # chunks retrieved into the prompt; None when RAG was not requested
    eligibility: str | None = None  # cheap could-have-run-local verdict; null when telemetry off
